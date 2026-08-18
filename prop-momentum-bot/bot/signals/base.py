"""Strategy interface and the indicator cache strategies read from.

Design note: indicators are computed over the whole bar array and strategies
index into them at bar `i`. That is only safe because every indicator in
`bot.indicators` is causal — value[i] depends on bars 0..i and nothing later.
`tests/test_indicators.py` asserts that property directly, because it is the
assumption the entire backtest rests on.

The live loop uses the same class over a trailing window, so a strategy cannot
behave differently in backtest and live.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from .. import indicators as ind
from ..config import Instrument, StrategyParams
from ..data.types import Bar, Side, Signal


@dataclass
class IndicatorCache:
    """Vectorised indicator series for one symbol at one timeframe."""

    symbol: str
    bars: List[Bar]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    series: Dict[str, np.ndarray] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.bars)

    def get(self, name: str) -> np.ndarray:
        return self.series[name]

    def at(self, name: str, i: int) -> float:
        arr = self.series.get(name)
        if arr is None or i >= len(arr):
            return float("nan")
        return float(arr[i])

    def ready(self, i: int, names: Sequence[str]) -> bool:
        """True when every named series has a real value at bar i."""
        if i < 1:
            return False
        for n in names:
            v = self.at(n, i)
            if v != v:  # NaN
                return False
        return True

    @classmethod
    def build(cls, symbol: str, bars: List[Bar], instrument: Instrument, params: StrategyParams) -> "IndicatorCache":
        o = np.array([b.open for b in bars], dtype=float)
        h = np.array([b.high for b in bars], dtype=float)
        l = np.array([b.low for b in bars], dtype=float)
        c = np.array([b.close for b in bars], dtype=float)
        v = np.array([b.volume for b in bars], dtype=float)
        cache = cls(symbol, bars, o, h, l, c, v)

        tz = ZoneInfo(instrument.session_tz)
        session_ids = np.array(
            [int(b.ts.astimezone(tz).date().toordinal()) for b in bars], dtype=np.int64
        )
        cache.series["session_id"] = session_ids

        p = params
        cache.series["ema_fast"] = ind.ema(c, p.geti("ema_fast", 9))
        cache.series["ema_slow"] = ind.ema(c, p.geti("ema_slow", 21))
        cache.series["ema_trend"] = ind.ema(c, p.geti("ema_trend", 50))
        cache.series["atr"] = ind.atr(h, l, c, p.geti("atr_period", 14))
        adx_v, plus_di, minus_di = ind.adx(h, l, c, p.geti("adx_period", 14))
        cache.series["adx"] = adx_v
        cache.series["plus_di"] = plus_di
        cache.series["minus_di"] = minus_di
        cache.series["vwap"] = ind.session_vwap(h, l, c, v, session_ids)
        cache.series["rsi"] = ind.rsi(c, p.geti("rsi_period", 14))
        if p.get("donchian_period"):
            up, dn = ind.donchian(h, l, p.geti("donchian_period", 20))
            cache.series["donchian_up"] = up
            cache.series["donchian_dn"] = dn
        cache.series["avg_volume"] = ind.rolling_mean_volume(v, 20)
        return cache


class Strategy(ABC):
    """Produces entry signals. Knows nothing about account state or sizing —
    that separation is what lets the risk guard be the only gatekeeper."""

    name: str = "strategy"

    def __init__(self, params: StrategyParams, instrument: Instrument) -> None:
        # Resolve per-asset-class overrides once, at construction, so every
        # read of self.params sees thresholds appropriate to this instrument.
        self.params = params.for_asset_class(instrument.asset_class)
        self.instrument = instrument

    @property
    def timeframe_minutes(self) -> int:
        return self.params.timeframe_minutes

    def build_cache(self, bars: List[Bar]) -> IndicatorCache:
        return IndicatorCache.build(self.instrument.symbol, bars, self.instrument, self.params)

    @property
    def resolved_params(self) -> StrategyParams:
        """Parameters after asset-class overrides. Exposed for reporting."""
        return self.params

    @abstractmethod
    def on_bar(self, cache: IndicatorCache, i: int) -> Optional[Signal]:
        """Inspect bar `i` and optionally return an entry signal.

        Implementations must only read cache values at indices <= i.
        """

    # -- shared helpers -------------------------------------------------

    def _volatility_ok(self, cache: IndicatorCache, i: int) -> bool:
        """Reject both dead tape and news-spike chaos."""
        atr = cache.at("atr", i)
        price = cache.close[i]
        if atr != atr or price <= 0:
            return False
        atr_pct = atr / price
        return (
            self.params.getf("min_atr_pct_of_price", 0.0)
            <= atr_pct
            <= self.params.getf("max_atr_pct_of_price", 1.0)
        )

    def _trend_bias(self, cache: IndicatorCache, i: int) -> Optional[Side]:
        """Directional bias from the EMA stack. None means no clean bias."""
        fast, slow, trend = (
            cache.at("ema_fast", i),
            cache.at("ema_slow", i),
            cache.at("ema_trend", i),
        )
        if any(x != x for x in (fast, slow, trend)):
            return None
        price = cache.close[i]
        if fast > slow > trend and price > trend:
            return Side.LONG
        if fast < slow < trend and price < trend:
            return Side.SHORT
        return None

    def _adx_ok(self, cache: IndicatorCache, i: int) -> bool:
        adx = cache.at("adx", i)
        if adx != adx:
            return False
        return adx >= self.params.getf("adx_min", 0.0)

    def _make_signal(
        self,
        cache: IndicatorCache,
        i: int,
        side: Side,
        entry: float,
        atr: float,
        reason: str,
        **meta,
    ) -> Signal:
        """Attach ATR-derived stop and target geometry to an entry."""
        stop_mult = self.params.getf("stop_atr_mult", 1.5)
        target_mult = self.params.getf("target_atr_mult", 3.0)
        inst = self.instrument
        stop = entry - side.sign * stop_mult * atr
        target = entry + side.sign * target_mult * atr
        return Signal(
            ts=cache.bars[i].ts,
            symbol=inst.symbol,
            side=side,
            strategy=self.name,
            entry_price=inst.round_price(entry),
            stop_price=inst.round_price(stop),
            target_price=inst.round_price(target),
            atr=atr,
            reason=reason,
            meta={"bar_index": i, **meta},
        )
