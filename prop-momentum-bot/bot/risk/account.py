"""Account state: equity, high-water marks, and trading-day boundaries.

The trading day is NOT the calendar day. A futures firm rolls at 17:00 America/
New_York; an FX firm rolls at midnight UTC. Getting this wrong means the daily
loss limit resets at the wrong moment, which is the classic way an otherwise
sound bot fails an evaluation on a technicality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from ..config import FirmRules
from ..data.types import AccountSnapshot, Trade
from .rules import DrawdownFloor, FloorState, RuleCheck, check_evaluation_status, make_floor


def trading_day_of(ts: datetime, tz_name: str, reset_hour: int) -> date:
    """The trading day `ts` belongs to, given a session roll hour.

    With reset_hour=17, a bar at 18:00 Monday belongs to Tuesday's session —
    exactly how CME dates its overnight tape.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(ZoneInfo(tz_name))
    if reset_hour and local.hour >= reset_hour:
        local = local + timedelta(days=1)
    return local.date()


@dataclass
class DayRecord:
    """Per-trading-day accounting, used by the daily limit and consistency rule."""

    day: date
    start_balance: float
    end_balance: Optional[float] = None
    realized: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    lowest_equity: float = float("inf")
    highest_equity: float = float("-inf")
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: Optional[str] = None

    @property
    def profit(self) -> float:
        return self.realized


class Account:
    """Tracks balance, equity and every high-water mark the firm rules read.

    `mark()` is called on every bar with current unrealized PnL; `on_trade_closed()`
    is called when a round turn completes. Between them they maintain enough
    state to answer "am I still in this evaluation?" at any instant.
    """

    def __init__(self, rules: FirmRules) -> None:
        self.rules = rules
        self.floor_model: DrawdownFloor = make_floor(rules)

        self.balance = rules.starting_balance
        self.unrealized = 0.0
        self.peak_equity = rules.starting_balance
        self.peak_eod_balance = rules.starting_balance
        self.min_equity = rules.starting_balance

        self.days: Dict[date, DayRecord] = {}
        self.day_order: List[date] = []
        self.current_day: Optional[date] = None

        self.trades: List[Trade] = []
        self.consecutive_losses = 0
        self.consecutive_wins = 0

        self.status = RuleCheck()
        self.failed_reason: Optional[str] = None
        self.passed = False
        self.last_ts: Optional[datetime] = None

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @property
    def equity(self) -> float:
        return self.balance + self.unrealized

    @property
    def total_profit(self) -> float:
        return self.balance - self.rules.starting_balance

    @property
    def floor_state(self) -> FloorState:
        return FloorState(
            starting_balance=self.rules.starting_balance,
            peak_equity=self.peak_equity,
            peak_eod_balance=self.peak_eod_balance,
        )

    @property
    def floor(self) -> float:
        return self.floor_model.floor(self.floor_state)

    @property
    def buffer(self) -> float:
        """Dollars of room above the bust line."""
        return max(0.0, self.equity - self.floor)

    @property
    def day(self) -> DayRecord:
        if self.current_day is None:
            raise RuntimeError("no trading day started; call mark() before reading day state")
        return self.days[self.current_day]

    @property
    def daily_loss_used(self) -> float:
        """How much of today's loss allowance is consumed, in dollars."""
        if not self.rules.has_daily_limit or self.current_day is None:
            return 0.0
        reference = self.equity if self.rules.daily_loss_includes_unrealized else self.balance
        return max(0.0, self.day.start_balance - reference)

    @property
    def daily_buffer(self) -> float:
        if not self.rules.has_daily_limit:
            return float("inf")
        return max(0.0, self.rules.max_daily_loss - self.daily_loss_used)

    @property
    def day_consecutive_losses(self) -> int:
        """Loss streak within the current session only.

        The "stop for the day" rule reads this rather than the running streak:
        a tilt-stop is a session circuit breaker, and carrying it across the
        day boundary would halt the bot permanently after one bad afternoon.
        """
        if self.current_day is None:
            return 0
        return self.days[self.current_day].consecutive_losses

    @property
    def trading_days(self) -> int:
        """Days on which at least one trade was closed — how firms count them."""
        return sum(1 for d in self.days.values() if d.trades > 0)

    @property
    def best_day_profit(self) -> float:
        profits = [d.profit for d in self.days.values() if d.profit > 0]
        return max(profits) if profits else 0.0

    @property
    def progress(self) -> float:
        """Fraction of the profit target achieved (clamped at 0)."""
        target = self.rules.profit_target
        if target <= 0:
            return 0.0
        return max(0.0, self.total_profit / target)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def mark(self, ts: datetime, unrealized: float) -> RuleCheck:
        """Update equity for the current bar and re-evaluate the firm's rules."""
        self.last_ts = ts
        day = trading_day_of(ts, self.rules.daily_reset_tz, self.rules.daily_reset_hour)
        if day != self.current_day:
            self._roll_day(day)

        self.unrealized = unrealized
        eq = self.equity

        # High-water marks. Intraday trailing reads peak_equity, so it must be
        # updated on every mark, not just on trade close.
        if eq > self.peak_equity:
            self.peak_equity = eq
        if eq < self.min_equity:
            self.min_equity = eq

        rec = self.days[self.current_day]
        rec.lowest_equity = min(rec.lowest_equity, eq)
        rec.highest_equity = max(rec.highest_equity, eq)

        self.status = check_evaluation_status(
            rules=self.rules,
            floor=self.floor_model,
            state=self.floor_state,
            equity=eq,
            balance=self.balance,
            realized_today=rec.realized,
            day_start_balance=rec.start_balance,
            trading_days=self.trading_days,
            best_day_profit=self.best_day_profit,
            total_profit=self.total_profit,
        )
        if self.status.failed and self.failed_reason is None:
            self.failed_reason = self.status.reason
        if self.status.passed:
            self.passed = True
        return self.status

    def _roll_day(self, new_day: date) -> None:
        """Close the previous trading day and open a new one."""
        if self.current_day is not None:
            prev = self.days[self.current_day]
            prev.end_balance = self.balance
            # The EOD trailing floor only ratchets on a settled daily balance.
            if self.balance > self.peak_eod_balance:
                self.peak_eod_balance = self.balance
        self.current_day = new_day
        if new_day not in self.days:
            self.days[new_day] = DayRecord(day=new_day, start_balance=self.balance)
            self.day_order.append(new_day)

    def on_trade_closed(self, trade: Trade) -> None:
        """Book a completed round turn into balance and the daily record."""
        self.balance += trade.net_pnl
        self.trades.append(trade)

        if self.current_day is None:
            self._roll_day(trading_day_of(trade.exit_ts, self.rules.daily_reset_tz, self.rules.daily_reset_hour))
        rec = self.days[self.current_day]
        rec.realized += trade.net_pnl
        rec.trades += 1

        if trade.is_win:
            rec.wins += 1
            rec.consecutive_losses = 0
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            rec.losses += 1
            rec.consecutive_losses += 1
            self.consecutive_losses += 1
            self.consecutive_wins = 0

        # A static floor never trails, but peak_eod_balance is cheap to keep
        # accurate for reporting regardless of model.
        if self.balance > self.peak_eod_balance and self.rules.drawdown_model == "static":
            self.peak_eod_balance = self.balance

    def finalize(self) -> None:
        """Settle the final day. Call once the run is over."""
        if self.current_day is not None:
            rec = self.days[self.current_day]
            if rec.end_balance is None:
                rec.end_balance = self.balance
            if self.balance > self.peak_eod_balance:
                self.peak_eod_balance = self.balance

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def snapshot(self, open_positions: int = 0) -> AccountSnapshot:
        rec = self.days.get(self.current_day) if self.current_day else None
        return AccountSnapshot(
            ts=self.last_ts or datetime.now(timezone.utc),
            balance=self.balance,
            equity=self.equity,
            day_start_balance=rec.start_balance if rec else self.rules.starting_balance,
            day_start_equity=rec.start_balance if rec else self.rules.starting_balance,
            peak_equity=self.peak_equity,
            peak_eod_balance=self.peak_eod_balance,
            realized_today=rec.realized if rec else 0.0,
            unrealized=self.unrealized,
            trades_today=rec.trades if rec else 0,
            consecutive_losses=self.consecutive_losses,
            trading_days=self.trading_days,
            open_positions=open_positions,
            halted=self.status.failed,
            halt_reason=self.failed_reason,
        )

    def describe(self) -> str:
        return (
            f"balance ${self.balance:,.2f} | equity ${self.equity:,.2f} | "
            f"floor ${self.floor:,.2f} | buffer ${self.buffer:,.2f} | "
            f"progress {self.progress * 100:.1f}% | days {self.trading_days}"
        )
