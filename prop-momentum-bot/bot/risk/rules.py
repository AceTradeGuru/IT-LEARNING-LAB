"""Drawdown floor models.

The "floor" is the equity level at which the evaluation is failed. Where that
line sits, and whether it moves, is the single biggest structural difference
between prop firms — and it changes correct behaviour completely:

* static           — the floor never moves. Profit is a permanent buffer, so
                     the account gets safer as it wins. (FTMO-style.)
* eod_trailing     — the floor follows the highest END-OF-DAY balance. Intraday
                     profit does not raise it, so giving back an intraday gain
                     costs nothing against the floor, but banking it does raise
                     the floor for tomorrow. (Topstep-style.)
* intraday_trailing— the floor follows peak equity INCLUDING open profit. A
                     trade that goes +$800 and comes back to flat has
                     permanently cost $800 of buffer. This model punishes
                     round-tripping harder than anything else in prop trading,
                     which is why the engine trails stops aggressively under it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from ..config import FirmRules


@dataclass
class FloorState:
    """Mutable high-water marks the floor models read from."""

    starting_balance: float
    peak_equity: float
    peak_eod_balance: float


class DrawdownFloor(ABC):
    """Computes the bust level and how much room is left above it."""

    def __init__(self, rules: FirmRules) -> None:
        self.rules = rules
        self.max_drawdown = rules.max_total_drawdown
        self.freeze_at = rules.trail_freeze_at_balance

    @abstractmethod
    def floor(self, state: FloorState) -> float:
        """The equity level that fails the evaluation."""

    def buffer(self, state: FloorState, equity: float) -> float:
        """Dollars between current equity and the floor. Negative means bust."""
        return equity - self.floor(state)

    def _apply_freeze(self, raw_floor: float) -> float:
        """Many firms stop trailing once the floor reaches a set level (often
        breakeven, or start + target). Past that point the floor is fixed."""
        if self.freeze_at is None:
            return raw_floor
        frozen_ceiling = self.freeze_at - self.max_drawdown
        # Some firms freeze the floor AT a balance level rather than at
        # (level - dd); this models the common "trails to breakeven" variant
        # where the floor stops climbing once it reaches start balance.
        ceiling = max(frozen_ceiling, self.rules.starting_balance)
        return min(raw_floor, ceiling)

    @property
    def name(self) -> str:
        return self.__class__.__name__


class StaticFloor(DrawdownFloor):
    def floor(self, state: FloorState) -> float:
        return state.starting_balance - self.max_drawdown


class EodTrailingFloor(DrawdownFloor):
    def floor(self, state: FloorState) -> float:
        anchor = max(state.peak_eod_balance, state.starting_balance)
        return self._apply_freeze(anchor - self.max_drawdown)


class IntradayTrailingFloor(DrawdownFloor):
    def floor(self, state: FloorState) -> float:
        anchor = max(state.peak_equity, state.starting_balance)
        return self._apply_freeze(anchor - self.max_drawdown)


_MODELS = {
    "static": StaticFloor,
    "eod_trailing": EodTrailingFloor,
    "intraday_trailing": IntradayTrailingFloor,
}


def make_floor(rules: FirmRules) -> DrawdownFloor:
    try:
        return _MODELS[rules.drawdown_model](rules)
    except KeyError as exc:
        raise ValueError(f"unsupported drawdown_model {rules.drawdown_model!r}") from exc


@dataclass
class RuleCheck:
    """Result of evaluating the firm's pass/fail conditions."""

    failed: bool = False
    passed: bool = False
    reason: str = ""

    @property
    def still_running(self) -> bool:
        return not self.failed and not self.passed


def check_evaluation_status(
    rules: FirmRules,
    floor: DrawdownFloor,
    state: FloorState,
    equity: float,
    balance: float,
    realized_today: float,
    day_start_balance: float,
    trading_days: int,
    best_day_profit: float,
    total_profit: float,
) -> RuleCheck:
    """Evaluate every hard rule at once and report the first breach.

    Ordering matters: failures are checked before the pass condition, because
    an account that hits the target and the drawdown floor in the same tick has
    failed, not passed.
    """
    # --- total drawdown ---
    if equity <= floor.floor(state):
        return RuleCheck(
            failed=True,
            reason=(
                f"max drawdown breached: equity ${equity:,.2f} <= "
                f"floor ${floor.floor(state):,.2f} ({rules.drawdown_model})"
            ),
        )

    # --- daily loss ---
    if rules.has_daily_limit:
        reference = equity if rules.daily_loss_includes_unrealized else balance
        day_loss = day_start_balance - reference
        if day_loss >= rules.max_daily_loss:
            return RuleCheck(
                failed=True,
                reason=f"daily loss limit breached: -${day_loss:,.2f} vs ${rules.max_daily_loss:,.2f}",
            )

    # --- max days ---
    if rules.max_trading_days and trading_days > rules.max_trading_days:
        return RuleCheck(
            failed=True,
            reason=f"exceeded max trading days ({trading_days} > {rules.max_trading_days})",
        )

    # --- pass conditions: target AND min days AND consistency ---
    if total_profit >= rules.profit_target:
        if trading_days < rules.min_trading_days:
            return RuleCheck(
                reason=(
                    f"target reached but {trading_days}/{rules.min_trading_days} "
                    "minimum trading days completed"
                )
            )
        if rules.enforces_consistency and total_profit > 0:
            share = 100.0 * best_day_profit / total_profit
            if share > rules.consistency_max_day_pct:
                return RuleCheck(
                    reason=(
                        f"target reached but consistency rule unmet: best day is "
                        f"{share:.1f}% of profit (max {rules.consistency_max_day_pct:.0f}%)"
                    )
                )
        return RuleCheck(passed=True, reason=f"profit target ${rules.profit_target:,.2f} reached")

    return RuleCheck()


def required_profit_for_consistency(rules: FirmRules, best_day_profit: float) -> float:
    """Total profit needed before `best_day_profit` satisfies the consistency rule.

    Useful to the guard: if one day ran hot, the account must grind out more
    profit on other days before the target actually counts as passed.
    """
    if not rules.enforces_consistency or best_day_profit <= 0:
        return 0.0
    return best_day_profit / (rules.consistency_max_day_pct / 100.0)
