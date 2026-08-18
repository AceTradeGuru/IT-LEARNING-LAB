"""Performance statistics.

Deliberately includes the unflattering ones. Win rate and total profit are the
numbers people quote; expectancy, cost drag, and the R-distribution are the
numbers that tell you whether an edge exists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence

from ..data.types import ExitReason, Trade


@dataclass
class Metrics:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    total_costs: float = 0.0
    net_pnl: float = 0.0
    profit_factor: float = 0.0
    expectancy_r: float = 0.0
    expectancy_dollars: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    payoff_ratio: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe: float = 0.0
    sortino: float = 0.0
    avg_bars_held: float = 0.0
    cost_drag_pct: float = 0.0
    by_exit_reason: Dict[str, int] = field(default_factory=dict)
    by_strategy: Dict[str, dict] = field(default_factory=dict)
    by_symbol: Dict[str, dict] = field(default_factory=dict)

    @property
    def edge_verdict(self) -> str:
        """A blunt read on whether these numbers represent an edge."""
        if self.trades < 30:
            return "insufficient sample — need 100+ trades before believing anything"
        if self.profit_factor >= 1.3 and self.expectancy_r > 0.08:
            return "positive expectancy, worth forward-testing"
        if self.profit_factor >= 1.0:
            return "marginal — costs are eating most of the edge"
        return "negative expectancy — do not trade this configuration"


def _streaks(trades: Sequence[Trade]) -> tuple[int, int]:
    max_l = max_w = cur_l = cur_w = 0
    for t in trades:
        if t.is_win:
            cur_w += 1
            cur_l = 0
        else:
            cur_l += 1
            cur_w = 0
        max_l = max(max_l, cur_l)
        max_w = max(max_w, cur_w)
    return max_l, max_w


def _subset_stats(trades: Sequence[Trade]) -> dict:
    if not trades:
        return {"trades": 0, "net": 0.0, "win_rate": 0.0, "expectancy_r": 0.0}
    wins = [t for t in trades if t.is_win]
    net = sum(t.net_pnl for t in trades)
    return {
        "trades": len(trades),
        "net": net,
        "win_rate": 100.0 * len(wins) / len(trades),
        "expectancy_r": sum(t.r_multiple for t in trades) / len(trades),
        "avg_net": net / len(trades),
    }


def compute(trades: Sequence[Trade], equity_curve: Optional[Sequence] = None) -> Metrics:
    m = Metrics()
    m.trades = len(trades)
    if not trades:
        return m

    wins = [t for t in trades if t.is_win]
    losses = [t for t in trades if not t.is_win]
    m.wins, m.losses = len(wins), len(losses)
    m.win_rate = 100.0 * m.wins / m.trades

    m.gross_profit = sum(t.net_pnl for t in wins)
    m.gross_loss = abs(sum(t.net_pnl for t in losses))
    m.total_costs = sum(t.costs for t in trades)
    m.net_pnl = sum(t.net_pnl for t in trades)
    m.profit_factor = (m.gross_profit / m.gross_loss) if m.gross_loss > 0 else float("inf")

    m.expectancy_r = sum(t.r_multiple for t in trades) / m.trades
    m.expectancy_dollars = m.net_pnl / m.trades
    m.avg_win = m.gross_profit / m.wins if m.wins else 0.0
    m.avg_loss = m.gross_loss / m.losses if m.losses else 0.0
    m.payoff_ratio = (m.avg_win / m.avg_loss) if m.avg_loss > 0 else float("inf")
    m.largest_win = max((t.net_pnl for t in trades), default=0.0)
    m.largest_loss = min((t.net_pnl for t in trades), default=0.0)
    m.max_consecutive_losses, m.max_consecutive_wins = _streaks(trades)
    m.avg_bars_held = sum(t.bars_held for t in trades) / m.trades

    gross_total = sum(t.gross_pnl for t in trades)
    m.cost_drag_pct = (
        100.0 * m.total_costs / abs(gross_total) if gross_total else float("inf") if m.total_costs else 0.0
    )

    # Per-trade returns, used for Sharpe/Sortino on a trade basis.
    rs = [t.r_multiple for t in trades]
    mean_r = sum(rs) / len(rs)
    var = sum((r - mean_r) ** 2 for r in rs) / (len(rs) - 1) if len(rs) > 1 else 0.0
    sd = math.sqrt(var)
    m.sharpe = (mean_r / sd * math.sqrt(len(rs))) if sd > 0 else 0.0
    downside = [r for r in rs if r < 0]
    if downside:
        d_var = sum(r ** 2 for r in downside) / len(downside)
        d_sd = math.sqrt(d_var)
        m.sortino = (mean_r / d_sd * math.sqrt(len(rs))) if d_sd > 0 else 0.0

    for t in trades:
        key = t.exit_reason.value
        m.by_exit_reason[key] = m.by_exit_reason.get(key, 0) + 1

    for name in {t.strategy for t in trades}:
        m.by_strategy[name] = _subset_stats([t for t in trades if t.strategy == name])
    for sym in {t.symbol for t in trades}:
        m.by_symbol[sym] = _subset_stats([t for t in trades if t.symbol == sym])

    if equity_curve:
        peak = -float("inf")
        max_dd = 0.0
        peak_val = 0.0
        for pt in equity_curve:
            eq = pt.equity
            if eq > peak:
                peak = eq
                peak_val = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd
        m.max_drawdown = max_dd
        m.max_drawdown_pct = 100.0 * max_dd / peak_val if peak_val else 0.0

    return m


@dataclass
class EvaluationStats:
    """Aggregate outcome across many simulated evaluation attempts."""

    runs: int
    passed: int
    failed: int
    incomplete: int
    pass_rate: float
    fail_rate: float
    median_days_to_pass: Optional[float]
    avg_final_balance: float
    worst_final_balance: float
    best_final_balance: float
    avg_max_drawdown: float
    pass_ci_low: float = 0.0
    pass_ci_high: float = 0.0

    def summary(self) -> str:
        return (
            f"{self.runs} simulated evaluations: {self.pass_rate:.1f}% passed "
            f"[95% CI {self.pass_ci_low:.1f}–{self.pass_ci_high:.1f}%], "
            f"{self.fail_rate:.1f}% blown, "
            f"{100 - self.pass_rate - self.fail_rate:.1f}% ran out of time"
        )


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval — honest error bars on a pass rate.

    A 60% pass rate from 20 runs and from 2000 runs are very different claims,
    and a point estimate hides that. This makes the uncertainty visible.
    """
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z ** 2 / n
    centre = (p + z ** 2 / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z ** 2 / (4 * n ** 2)) / denom
    return max(0.0, 100 * (centre - margin)), min(100.0, 100 * (centre + margin))


def aggregate(results: Sequence) -> EvaluationStats:
    n = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if r.failed)
    incomplete = n - passed - failed
    balances = [r.account.balance for r in results]
    dds = []
    days_to_pass = []
    for r in results:
        mm = compute(r.trades, r.equity_curve)
        dds.append(mm.max_drawdown)
        if r.passed:
            days_to_pass.append(r.account.trading_days)
    days_to_pass.sort()
    median_days = (
        days_to_pass[len(days_to_pass) // 2] if days_to_pass else None
    )
    lo, hi = wilson_interval(passed, n)
    return EvaluationStats(
        runs=n,
        passed=passed,
        failed=failed,
        incomplete=incomplete,
        pass_rate=100.0 * passed / n if n else 0.0,
        fail_rate=100.0 * failed / n if n else 0.0,
        median_days_to_pass=median_days,
        avg_final_balance=sum(balances) / n if n else 0.0,
        worst_final_balance=min(balances) if balances else 0.0,
        best_final_balance=max(balances) if balances else 0.0,
        avg_max_drawdown=sum(dds) / len(dds) if dds else 0.0,
        pass_ci_low=lo,
        pass_ci_high=hi,
    )
