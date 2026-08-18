"""The risk guard: the single place a trade can be vetoed.

Every order in this system passes through `RiskGuard.evaluate()`. Strategies
propose; the guard disposes. Keeping the veto in one place means the firm's
rules are enforced identically in backtest, paper and live — there is no path
to the broker that skips these checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from ..config import BotConfig, Instrument
from ..data.types import Position, RiskDecision, Signal, Side
from .account import Account
from .rules import required_profit_for_consistency
from .sizing import size_position


@dataclass
class SessionWindow:
    """Whether an instrument is tradeable at a given instant."""

    open_ok: bool
    minutes_to_close: float
    reason: str = ""


def session_window(instrument: Instrument, ts: datetime, flatten_before_close: int) -> SessionWindow:
    """Where `ts` sits inside the instrument's trading session."""
    local = ts.astimezone(ZoneInfo(instrument.session_tz))
    if instrument.is_24h:
        return SessionWindow(True, float("inf"))

    now_minutes = local.hour * 60 + local.minute
    open_minutes = instrument.session_open.hour * 60 + instrument.session_open.minute
    close_minutes = instrument.session_close.hour * 60 + instrument.session_close.minute

    if close_minutes <= open_minutes:  # session wraps midnight
        inside = now_minutes >= open_minutes or now_minutes < close_minutes
        to_close = (close_minutes - now_minutes) % (24 * 60)
    else:
        inside = open_minutes <= now_minutes < close_minutes
        to_close = close_minutes - now_minutes

    if not inside:
        return SessionWindow(False, to_close, f"outside {instrument.symbol} session")
    if to_close <= flatten_before_close:
        return SessionWindow(False, to_close, f"within {flatten_before_close}m of session close")
    return SessionWindow(True, to_close)


class RiskGuard:
    """Applies firm rules, portfolio limits and sizing to every proposed trade."""

    def __init__(self, config: BotConfig, account: Account) -> None:
        self.config = config
        self.account = account
        self.rules = config.firm
        self.risk_cfg = config.risk
        self.portfolio_cfg = config.portfolio
        self.session_halt_reason: Optional[str] = None
        self.halted_day = None
        self.news_windows: List[tuple[datetime, datetime]] = []

    # ------------------------------------------------------------------
    # halts
    # ------------------------------------------------------------------

    def load_news_blackouts(self, events: Iterable[datetime]) -> None:
        """Register high-impact news times; trading is blocked around each."""
        before = timedelta(minutes=self.risk_cfg.news_blackout_minutes_before)
        after = timedelta(minutes=self.risk_cfg.news_blackout_minutes_after)
        self.news_windows = [(e - before, e + after) for e in events]

    def _in_news_blackout(self, ts: datetime) -> bool:
        return any(start <= ts <= end for start, end in self.news_windows)

    def check_halt(self) -> Optional[str]:
        """Reasons to stop opening new trades for the rest of the day/eval.

        These are deliberately tighter than the firm's actual limits. Stopping
        at 60% of the daily allowance leaves room for an open position to move
        against you while it is being closed; stopping at 100% does not.
        """
        acct = self.account

        if acct.status.failed:
            return f"evaluation failed: {acct.failed_reason}"

        if self.risk_cfg.stop_on_target_reached and acct.total_profit >= self.rules.profit_target:
            # Only actually stop if the pass is real — min days and the
            # consistency rule can both still be outstanding.
            if acct.trading_days >= self.rules.min_trading_days:
                needed = required_profit_for_consistency(self.rules, acct.best_day_profit)
                if not self.rules.enforces_consistency or acct.total_profit >= needed:
                    return "profit target reached — protecting the pass"

        floor_used = 1.0 - (acct.buffer / self.rules.max_total_drawdown)
        if floor_used >= self.risk_cfg.total_drawdown_stop_fraction:
            return (
                f"drawdown buffer {floor_used:.0%} consumed "
                f"(stop at {self.risk_cfg.total_drawdown_stop_fraction:.0%})"
            )

        if self.rules.has_daily_limit:
            used = acct.daily_loss_used / self.rules.max_daily_loss
            if used >= self.risk_cfg.daily_loss_stop_fraction:
                return (
                    f"daily loss {used:.0%} of limit "
                    f"(stop at {self.risk_cfg.daily_loss_stop_fraction:.0%})"
                )

        if acct.day_consecutive_losses >= self.portfolio_cfg.max_consecutive_losses:
            return (
                f"{acct.day_consecutive_losses} consecutive losses today — "
                "stopping for the session"
            )

        if acct.current_day is not None:
            if acct.day.trades >= self.portfolio_cfg.max_trades_per_day:
                return f"daily trade cap reached ({self.portfolio_cfg.max_trades_per_day})"

        return None

    # ------------------------------------------------------------------
    # the veto
    # ------------------------------------------------------------------

    def evaluate(
        self,
        signal: Signal,
        open_positions: Dict[str, Position],
        ts: Optional[datetime] = None,
    ) -> RiskDecision:
        """Approve a signal with a size, or reject it with a reason."""
        ts = ts or signal.ts
        acct = self.account
        instrument = self.config.instruments.get(signal.symbol)
        if instrument is None:
            return RiskDecision.reject(f"unknown instrument {signal.symbol}")

        if instrument.asset_class not in self.rules.asset_classes:
            return RiskDecision.reject(
                f"{instrument.asset_class} not permitted by {self.rules.key}"
            )

        halt = self.check_halt()
        if halt:
            return RiskDecision.reject(f"halted: {halt}")

        # --- personal trading window ---
        # Checked before the instrument session because it is the tighter of
        # the two and gives the clearer rejection message.
        personal = self.risk_cfg.trading_window
        if personal is not None and not personal.can_enter(ts):
            return RiskDecision.reject(
                f"outside personal trading window ({personal.describe()})"
            )

        # --- session / calendar ---
        window = session_window(instrument, ts, self.risk_cfg.flatten_before_close_minutes)
        if not window.open_ok:
            return RiskDecision.reject(window.reason or "outside session")

        if not self.rules.allow_weekend and ts.weekday() >= 5 and instrument.asset_class != "crypto":
            return RiskDecision.reject("weekend trading not permitted")

        if self._in_news_blackout(ts):
            return RiskDecision.reject("high-impact news blackout")

        # --- portfolio limits ---
        if len(open_positions) >= self.portfolio_cfg.max_concurrent_positions:
            return RiskDecision.reject(
                f"max concurrent positions ({self.portfolio_cfg.max_concurrent_positions})"
            )

        same_symbol = sum(1 for p in open_positions.values() if p.symbol == signal.symbol)
        if same_symbol >= self.portfolio_cfg.max_positions_per_instrument:
            return RiskDecision.reject(f"already in {signal.symbol}")

        group = self.portfolio_cfg.group_for(signal.symbol)
        if group:
            correlated = sum(
                1 for p in open_positions.values()
                if self.portfolio_cfg.group_for(p.symbol) == group
            )
            if correlated >= self.portfolio_cfg.max_correlated_positions:
                return RiskDecision.reject(
                    f"correlation cap for group '{group}' "
                    f"({self.portfolio_cfg.max_correlated_positions})"
                )

        # --- sizing ---
        sizing = size_position(signal, instrument, acct, self.risk_cfg)
        if not sizing.ok:
            return RiskDecision.reject(sizing.rejected_reason or "sizing failed", **(sizing.detail or {}))

        # --- worst case check ---
        # Assume the stop slips and every OTHER open position also stops out.
        # If that combined outcome would breach the floor, the trade is refused.
        if self.risk_cfg.reject_if_worst_case_breaches:
            open_risk = self._open_risk(open_positions)
            worst_case_equity = acct.equity - sizing.risk_amount - open_risk
            if worst_case_equity <= acct.floor:
                return RiskDecision.reject(
                    (
                        f"worst case ${worst_case_equity:,.2f} would breach floor "
                        f"${acct.floor:,.2f} (this trade ${sizing.risk_amount:,.2f} + "
                        f"open risk ${open_risk:,.2f})"
                    ),
                    worst_case_equity=worst_case_equity,
                )
            if self.rules.has_daily_limit:
                projected_day_loss = acct.daily_loss_used + sizing.risk_amount + open_risk
                if projected_day_loss >= self.rules.max_daily_loss:
                    return RiskDecision.reject(
                        (
                            f"worst case daily loss ${projected_day_loss:,.2f} would breach "
                            f"${self.rules.max_daily_loss:,.2f} limit"
                        ),
                        projected_day_loss=projected_day_loss,
                    )

        return RiskDecision(
            approved=True,
            size=sizing.size,
            risk_amount=sizing.risk_amount,
            reason=f"approved ({sizing.binding_constraint} budget)",
            detail={
                "binding": sizing.binding_constraint,
                "risk_pct": sizing.risk_pct_of_equity,
                "reward_risk": signal.reward_risk,
                **(sizing.detail or {}),
            },
        )

    def _open_risk(self, open_positions: Dict[str, Position]) -> float:
        """Total dollars still at risk across open positions, using live stops.

        Positions already trailed to breakeven or better contribute zero — and
        a position trailed into profit contributes NEGATIVE risk (locked gain),
        which is deliberately not counted, to keep the estimate conservative.
        """
        total = 0.0
        for pos in open_positions.values():
            instrument = self.config.instruments.get(pos.symbol)
            if instrument is None:
                continue
            adverse = (pos.entry_price - pos.stop_price) * pos.side.sign
            if adverse > 0:
                total += adverse * pos.size * instrument.value_per_point
        return total

    def should_flatten(self, instrument: Instrument, ts: datetime) -> bool:
        """True when open positions must be closed regardless of their stops."""
        # The personal window overrides the firm's overnight policy. If you are
        # not at the desk after noon, a position must not be live at 12:01 —
        # even on a firm that permits overnight holds.
        personal = self.risk_cfg.trading_window
        if personal is not None and personal.must_flatten(ts):
            return True
        if self.rules.allow_overnight:
            return False
        window = session_window(instrument, ts, self.risk_cfg.flatten_before_close_minutes)
        return not window.open_ok
