"""Typed configuration loading.

Everything tunable lives in config/*.yaml. This module turns that YAML into
frozen dataclasses so a typo becomes an import-time error instead of a silent
`None` deep inside the sizing math during a live session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from typing import Any, Dict, List, Optional

import yaml

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")


class ConfigError(ValueError):
    """Raised when a config file is missing a key the engine depends on."""


def _require(d: Dict[str, Any], key: str, ctx: str) -> Any:
    if key not in d:
        raise ConfigError(f"{ctx}: missing required key '{key}'")
    return d[key]


def _load_yaml(name: str, config_dir: Optional[str] = None) -> Dict[str, Any]:
    path = os.path.join(config_dir or CONFIG_DIR, name)
    if not os.path.exists(path):
        raise ConfigError(f"config file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"config file {path} did not parse to a mapping")
    return data


def _parse_hhmm(value: str, ctx: str) -> time:
    try:
        hh, mm = value.split(":")
        return time(int(hh), int(mm))
    except Exception as exc:  # noqa: BLE001 - want the context in the message
        raise ConfigError(f"{ctx}: bad HH:MM value {value!r}") from exc


# --------------------------------------------------------------------------
# Firm rules
# --------------------------------------------------------------------------

VALID_DRAWDOWN_MODELS = {"static", "eod_trailing", "intraday_trailing"}


@dataclass(frozen=True)
class FirmRules:
    key: str
    display_name: str
    asset_classes: List[str]
    starting_balance: float
    currency: str
    drawdown_model: str
    max_total_drawdown_pct: float
    max_daily_loss_pct: float
    profit_target_pct: float
    min_trading_days: int
    max_trading_days: int
    daily_reset_tz: str
    daily_reset_hour: int
    consistency_max_day_pct: float
    allow_overnight: bool
    allow_weekend: bool
    daily_loss_includes_unrealized: bool
    trail_freeze_at_balance: Optional[float] = None

    @property
    def max_total_drawdown(self) -> float:
        return self.starting_balance * self.max_total_drawdown_pct / 100.0

    @property
    def max_daily_loss(self) -> float:
        return self.starting_balance * self.max_daily_loss_pct / 100.0

    @property
    def profit_target(self) -> float:
        return self.starting_balance * self.profit_target_pct / 100.0

    @property
    def has_daily_limit(self) -> bool:
        return self.max_daily_loss_pct > 0

    @property
    def enforces_consistency(self) -> bool:
        return self.consistency_max_day_pct > 0

    @classmethod
    def from_dict(cls, key: str, d: Dict[str, Any]) -> "FirmRules":
        ctx = f"firms.yaml[{key}]"
        model = _require(d, "drawdown_model", ctx)
        if model not in VALID_DRAWDOWN_MODELS:
            raise ConfigError(
                f"{ctx}: drawdown_model {model!r} must be one of {sorted(VALID_DRAWDOWN_MODELS)}"
            )
        return cls(
            key=key,
            display_name=d.get("display_name", key),
            asset_classes=list(d.get("asset_classes", [])),
            starting_balance=float(_require(d, "starting_balance", ctx)),
            currency=d.get("currency", "USD"),
            drawdown_model=model,
            max_total_drawdown_pct=float(_require(d, "max_total_drawdown_pct", ctx)),
            max_daily_loss_pct=float(d.get("max_daily_loss_pct", 0.0)),
            profit_target_pct=float(_require(d, "profit_target_pct", ctx)),
            min_trading_days=int(d.get("min_trading_days", 0)),
            max_trading_days=int(d.get("max_trading_days", 0)),
            daily_reset_tz=d.get("daily_reset_tz", "UTC"),
            daily_reset_hour=int(d.get("daily_reset_hour", 0)),
            consistency_max_day_pct=float(d.get("consistency_max_day_pct", 0.0)),
            allow_overnight=bool(d.get("allow_overnight", True)),
            allow_weekend=bool(d.get("allow_weekend", False)),
            daily_loss_includes_unrealized=bool(d.get("daily_loss_includes_unrealized", True)),
            trail_freeze_at_balance=(
                float(d["trail_freeze_at_balance"])
                if d.get("trail_freeze_at_balance") is not None
                else None
            ),
        )


def load_firms(config_dir: Optional[str] = None) -> Dict[str, FirmRules]:
    raw = _load_yaml("firms.yaml", config_dir)
    profiles = _require(raw, "profiles", "firms.yaml")
    return {k: FirmRules.from_dict(k, v) for k, v in profiles.items()}


def load_firm(key: str, config_dir: Optional[str] = None) -> FirmRules:
    firms = load_firms(config_dir)
    if key not in firms:
        raise ConfigError(f"unknown firm profile {key!r}; available: {sorted(firms)}")
    return firms[key]


# --------------------------------------------------------------------------
# Instruments
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Instrument:
    symbol: str
    display_name: str
    asset_class: str
    tick_size: float
    value_per_point: float
    min_size: float
    size_step: float
    spread_points: float
    commission_per_unit_roundturn: float
    session_tz: str
    session_open: time
    session_close: time
    typical_atr_points: float
    taker_fee_bps: float = 0.0

    @property
    def is_24h(self) -> bool:
        return self.session_open == time(0, 0) and self.session_close >= time(23, 58)

    def round_size(self, size: float, round_down: bool = True) -> float:
        """Snap a raw size to the instrument's lot granularity."""
        if self.size_step <= 0:
            return size
        steps = size / self.size_step
        steps = int(steps) if round_down else round(steps)
        rounded = steps * self.size_step
        # Kill float dust (0.30000000000000004 lots is not a real order).
        rounded = round(rounded, 8)
        return rounded if rounded >= self.min_size else 0.0

    def round_price(self, price: float) -> float:
        if self.tick_size <= 0:
            return price
        return round(round(price / self.tick_size) * self.tick_size, 8)

    def cost_for(self, size: float, price: float) -> float:
        """Round-turn cost for `size` units: commission plus any bps fee."""
        commission = self.commission_per_unit_roundturn * size
        bps = 0.0
        if self.taker_fee_bps:
            notional = size * price * self.value_per_point
            bps = notional * (self.taker_fee_bps / 10000.0) * 2  # in and out
        return commission + bps

    @classmethod
    def from_dict(cls, symbol: str, d: Dict[str, Any]) -> "Instrument":
        ctx = f"instruments.yaml[{symbol}]"
        session = _require(d, "session", ctx)
        if not isinstance(session, (list, tuple)) or len(session) != 2:
            raise ConfigError(f"{ctx}: session must be a [open, close] pair")
        return cls(
            symbol=symbol,
            display_name=d.get("display_name", symbol),
            asset_class=_require(d, "asset_class", ctx),
            tick_size=float(_require(d, "tick_size", ctx)),
            value_per_point=float(_require(d, "value_per_point", ctx)),
            min_size=float(_require(d, "min_size", ctx)),
            size_step=float(_require(d, "size_step", ctx)),
            spread_points=float(d.get("spread_points", 0.0)),
            commission_per_unit_roundturn=float(d.get("commission_per_unit_roundturn", 0.0)),
            session_tz=d.get("session_tz", "UTC"),
            session_open=_parse_hhmm(session[0], ctx),
            session_close=_parse_hhmm(session[1], ctx),
            typical_atr_points=float(d.get("typical_atr_points", 0.0)),
            taker_fee_bps=float(d.get("taker_fee_bps", 0.0)),
        )


def load_instruments(config_dir: Optional[str] = None) -> Dict[str, Instrument]:
    raw = _load_yaml("instruments.yaml", config_dir)
    items = _require(raw, "instruments", "instruments.yaml")
    return {k: Instrument.from_dict(k, v) for k, v in items.items()}


# --------------------------------------------------------------------------
# Strategies
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StrategyParams:
    name: str
    enabled: bool
    timeframe_minutes: int
    params: Dict[str, Any] = field(default_factory=dict)

    def for_asset_class(self, asset_class: str) -> "StrategyParams":
        """Merge in any overrides declared for `asset_class`.

        Thresholds expressed as a fraction of price are not portable across
        asset classes — index futures and major FX pairs differ by roughly a
        factor of ten. Rather than pick one and quietly disable a whole market,
        each strategy may declare per-class overrides.
        """
        overrides = self.params.get("asset_class_overrides") or {}
        specific = overrides.get(asset_class)
        if not specific:
            return self
        merged = {k: v for k, v in self.params.items() if k != "asset_class_overrides"}
        merged.update(specific)
        return StrategyParams(
            name=self.name,
            enabled=self.enabled,
            timeframe_minutes=self.timeframe_minutes,
            params=merged,
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    def getf(self, key: str, default: float = 0.0) -> float:
        return float(self.params.get(key, default))

    def geti(self, key: str, default: int = 0) -> int:
        return int(self.params.get(key, default))


@dataclass(frozen=True)
class PortfolioParams:
    max_concurrent_positions: int
    max_positions_per_instrument: int
    max_correlated_positions: int
    correlation_groups: Dict[str, List[str]]
    max_consecutive_losses: int
    max_trades_per_day: int

    def group_for(self, symbol: str) -> Optional[str]:
        for group, symbols in self.correlation_groups.items():
            if symbol in symbols:
                return group
        return None


def load_strategies(config_dir: Optional[str] = None) -> tuple[Dict[str, StrategyParams], PortfolioParams]:
    raw = _load_yaml("strategies.yaml", config_dir)
    strat_raw = _require(raw, "strategies", "strategies.yaml")
    strategies = {}
    for name, d in strat_raw.items():
        strategies[name] = StrategyParams(
            name=name,
            enabled=bool(d.get("enabled", True)),
            timeframe_minutes=int(d.get("timeframe_minutes", 5)),
            params={k: v for k, v in d.items() if k not in ("enabled", "timeframe_minutes")},
        )
    p = raw.get("portfolio", {})
    portfolio = PortfolioParams(
        max_concurrent_positions=int(p.get("max_concurrent_positions", 3)),
        max_positions_per_instrument=int(p.get("max_positions_per_instrument", 1)),
        max_correlated_positions=int(p.get("max_correlated_positions", 2)),
        correlation_groups={k: list(v) for k, v in p.get("correlation_groups", {}).items()},
        max_consecutive_losses=int(p.get("max_consecutive_losses", 3)),
        max_trades_per_day=int(p.get("max_trades_per_day", 12)),
    )
    return strategies, portfolio


# --------------------------------------------------------------------------
# Risk
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskBand:
    up_to_progress: float
    risk_mult: float


@dataclass(frozen=True)
class RiskConfig:
    base_risk_pct: float
    max_risk_pct: float
    max_buffer_fraction_per_trade: float
    max_daily_buffer_fraction_per_trade: float
    min_stop_distance_atr: float
    max_cost_fraction_of_risk: float
    round_down_to_step: bool
    adaptive_enabled: bool
    bands: List[RiskBand]
    loss_streak_mult: List[float]
    defensive_daily_loss_fraction: float
    defensive_risk_mult: float
    daily_loss_stop_fraction: float
    total_drawdown_stop_fraction: float
    stop_on_target_reached: bool
    reject_if_worst_case_breaches: bool
    flatten_before_close_minutes: int
    news_blackout_minutes_before: int
    news_blackout_minutes_after: int

    def band_multiplier(self, progress: float) -> float:
        """Risk multiplier for how far through the profit target we are."""
        if not self.adaptive_enabled or not self.bands:
            return 1.0
        for band in self.bands:
            if progress <= band.up_to_progress:
                return band.risk_mult
        return self.bands[-1].risk_mult

    def streak_multiplier(self, consecutive_losses: int) -> float:
        if not self.adaptive_enabled or not self.loss_streak_mult:
            return 1.0
        idx = min(consecutive_losses, len(self.loss_streak_mult) - 1)
        return self.loss_streak_mult[idx]


def load_risk(config_dir: Optional[str] = None) -> RiskConfig:
    raw = _load_yaml("risk.yaml", config_dir)
    s = _require(raw, "sizing", "risk.yaml")
    a = raw.get("adaptive", {})
    g = raw.get("guards", {})
    bands = [
        RiskBand(float(b["up_to_progress"]), float(b["risk_mult"]))
        for b in a.get("bands", [])
    ]
    bands.sort(key=lambda b: b.up_to_progress)
    return RiskConfig(
        base_risk_pct=float(_require(s, "base_risk_pct", "risk.yaml[sizing]")),
        max_risk_pct=float(s.get("max_risk_pct", 1.0)),
        max_buffer_fraction_per_trade=float(s.get("max_buffer_fraction_per_trade", 0.10)),
        max_daily_buffer_fraction_per_trade=float(s.get("max_daily_buffer_fraction_per_trade", 0.25)),
        min_stop_distance_atr=float(s.get("min_stop_distance_atr", 0.5)),
        max_cost_fraction_of_risk=float(s.get("max_cost_fraction_of_risk", 0.15)),
        round_down_to_step=bool(s.get("round_down_to_step", True)),
        adaptive_enabled=bool(a.get("enabled", True)),
        bands=bands,
        loss_streak_mult=[float(x) for x in a.get("loss_streak_mult", [1.0])],
        defensive_daily_loss_fraction=float(a.get("defensive_daily_loss_fraction", 0.5)),
        defensive_risk_mult=float(a.get("defensive_risk_mult", 0.5)),
        daily_loss_stop_fraction=float(g.get("daily_loss_stop_fraction", 0.6)),
        total_drawdown_stop_fraction=float(g.get("total_drawdown_stop_fraction", 0.8)),
        stop_on_target_reached=bool(g.get("stop_on_target_reached", True)),
        reject_if_worst_case_breaches=bool(g.get("reject_if_worst_case_breaches", True)),
        flatten_before_close_minutes=int(g.get("flatten_before_close_minutes", 10)),
        news_blackout_minutes_before=int(g.get("news_blackout_minutes_before", 2)),
        news_blackout_minutes_after=int(g.get("news_blackout_minutes_after", 3)),
    )


@dataclass
class BotConfig:
    """One object carrying every piece of configuration the engine needs."""

    firm: FirmRules
    instruments: Dict[str, Instrument]
    strategies: Dict[str, StrategyParams]
    portfolio: PortfolioParams
    risk: RiskConfig

    @classmethod
    def load(cls, firm_key: str, config_dir: Optional[str] = None) -> "BotConfig":
        strategies, portfolio = load_strategies(config_dir)
        return cls(
            firm=load_firm(firm_key, config_dir),
            instruments=load_instruments(config_dir),
            strategies=strategies,
            portfolio=portfolio,
            risk=load_risk(config_dir),
        )

    def instruments_for_firm(self) -> Dict[str, Instrument]:
        """Only the instruments whose asset class the firm actually allows."""
        if not self.firm.asset_classes:
            return dict(self.instruments)
        return {
            k: v for k, v in self.instruments.items()
            if v.asset_class in self.firm.asset_classes
        }
