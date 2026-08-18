"""Position sizing.

The governing idea: risk per trade is the MINIMUM of several independent
budgets, not a fixed percentage. Each budget answers a different question —

    equity budget  : what would a sane trader risk on one idea?
    buffer budget  : what fraction of my remaining distance to the bust line
                     am I willing to spend on one idea?
    daily budget   : same question against today's loss limit.

Taking the minimum means the binding constraint is always whichever one is
closest to hurting. As the drawdown buffer shrinks, the buffer budget shrinks
with it and size collapses toward zero — a losing streak decays into the floor
rather than crashing through it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..config import Instrument, RiskConfig
from ..data.types import Signal
from .account import Account


@dataclass
class SizingResult:
    size: float
    risk_amount: float          # dollars at risk if the stop fills exactly
    risk_pct_of_equity: float
    binding_constraint: str     # which budget decided the size
    rejected_reason: Optional[str] = None
    detail: dict = None         # type: ignore[assignment]

    @property
    def ok(self) -> bool:
        return self.size > 0 and self.rejected_reason is None


def risk_per_unit(signal: Signal, instrument: Instrument) -> float:
    """Dollars lost per 1 unit if price travels from entry to stop."""
    return signal.stop_distance * instrument.value_per_point


def effective_risk_pct(cfg: RiskConfig, account: Account) -> tuple[float, str]:
    """Base risk %, scaled by evaluation progress, loss streak and daily damage."""
    pct = cfg.base_risk_pct
    notes = []

    band = cfg.band_multiplier(account.progress)
    if band != 1.0:
        pct *= band
        notes.append(f"progress {account.progress:.0%} x{band:g}")

    streak = cfg.streak_multiplier(account.consecutive_losses)
    if streak != 1.0:
        pct *= streak
        notes.append(f"{account.consecutive_losses} losses x{streak:g}")

    if account.rules.has_daily_limit and cfg.defensive_daily_loss_fraction > 0:
        used_fraction = account.daily_loss_used / account.rules.max_daily_loss
        if used_fraction >= cfg.defensive_daily_loss_fraction:
            pct *= cfg.defensive_risk_mult
            notes.append(f"daily loss {used_fraction:.0%} x{cfg.defensive_risk_mult:g}")

    pct = min(pct, cfg.max_risk_pct)
    return pct, ("; ".join(notes) if notes else "base")


def size_position(
    signal: Signal,
    instrument: Instrument,
    account: Account,
    cfg: RiskConfig,
) -> SizingResult:
    """Translate a signal into a tradeable size, or explain why it cannot be."""
    per_unit = risk_per_unit(signal, instrument)
    if per_unit <= 0:
        return SizingResult(0, 0, 0, "invalid", "stop distance is zero or negative")

    # A stop tighter than a fraction of ATR is usually a data glitch or a
    # setup that market noise will take out before the idea can work.
    if signal.atr > 0 and signal.stop_distance < cfg.min_stop_distance_atr * signal.atr:
        return SizingResult(
            0, 0, 0, "stop_too_tight",
            f"stop {signal.stop_distance:.5f} < {cfg.min_stop_distance_atr:g} ATR "
            f"({cfg.min_stop_distance_atr * signal.atr:.5f})",
        )

    pct, pct_note = effective_risk_pct(cfg, account)

    budgets: dict[str, float] = {
        "equity_pct": account.equity * pct / 100.0,
        "drawdown_buffer": account.buffer * cfg.max_buffer_fraction_per_trade,
    }
    if account.rules.has_daily_limit:
        budgets["daily_buffer"] = account.daily_buffer * cfg.max_daily_buffer_fraction_per_trade

    binding = min(budgets, key=lambda k: budgets[k])
    budget = budgets[binding]

    if budget <= 0:
        return SizingResult(
            0, 0, 0, binding,
            f"no risk budget left ({binding} = ${budget:,.2f})",
            detail=budgets,
        )

    raw_size = budget / per_unit
    size = instrument.round_size(raw_size, round_down=cfg.round_down_to_step)

    if size <= 0:
        return SizingResult(
            0, 0, 0, binding,
            (
                f"budget ${budget:,.2f} buys {raw_size:.4f} units, below the "
                f"{instrument.min_size:g} minimum for {instrument.symbol}"
            ),
            detail=budgets,
        )

    actual_risk = size * per_unit

    # Cost-to-risk gate. Round-turn commission and fees are known before the
    # trade is placed; if they consume too much of the risk budget the setup is
    # uneconomic regardless of how good the signal looks.
    if cfg.max_cost_fraction_of_risk > 0:
        cost = instrument.cost_for(size, signal.entry_price)
        if actual_risk > 0 and cost / actual_risk > cfg.max_cost_fraction_of_risk:
            return SizingResult(
                0, 0, 0, "cost_vs_risk",
                (
                    f"round-turn cost ${cost:,.2f} is {cost / actual_risk:.0%} of the "
                    f"${actual_risk:,.2f} risked (max {cfg.max_cost_fraction_of_risk:.0%}) — "
                    f"stop too tight for {instrument.symbol} costs"
                ),
                detail={**budgets, "cost": cost, "risk": actual_risk},
            )

    return SizingResult(
        size=size,
        risk_amount=actual_risk,
        risk_pct_of_equity=100.0 * actual_risk / account.equity if account.equity else 0.0,
        binding_constraint=binding,
        detail={**budgets, "risk_pct_applied": pct, "scaling": pct_note, "raw_size": raw_size},
    )
