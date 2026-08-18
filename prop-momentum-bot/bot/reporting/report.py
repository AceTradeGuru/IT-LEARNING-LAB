"""Human-readable reports.

Written to be read by someone deciding whether to risk money. That means the
failure modes get as much space as the returns, and every number that could be
mistaken for a promise is labelled with what produced it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Optional, Sequence

from ..config import BotConfig
from ..data.types import Trade
from .metrics import EvaluationStats, Metrics, compute


def _bar(value: float, width: int = 24, lo: float = 0.0, hi: float = 100.0) -> str:
    span = max(hi - lo, 1e-9)
    filled = int(round(width * max(0.0, min(1.0, (value - lo) / span))))
    return "█" * filled + "·" * (width - filled)


def _money(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def backtest_report(result, config: BotConfig, title: str = "BACKTEST") -> str:
    m = compute(result.trades, result.equity_curve)
    acct = result.account
    firm = config.firm
    L: List[str] = []

    L.append("=" * 78)
    L.append(f"  {title} — {firm.display_name}")
    L.append("=" * 78)

    if result.start_ts and result.end_ts:
        L.append(f"  Period      {result.start_ts:%Y-%m-%d} to {result.end_ts:%Y-%m-%d}"
                 f"  ({acct.trading_days} trading days)")
    L.append(f"  Strategies  {', '.join(sorted({t.strategy for t in result.trades})) or 'none traded'}")
    L.append(f"  Symbols     {', '.join(sorted({t.symbol for t in result.trades})) or '—'}")
    L.append("")

    # --- outcome ---
    verdict = {
        "PASSED": "PASSED — profit target met, all rules satisfied",
        "FAILED": f"FAILED — {acct.failed_reason}",
        "INCOMPLETE": "INCOMPLETE — survived, but did not reach the target in this window",
    }[result.outcome]
    L.append(f"  OUTCOME     {verdict}")
    L.append("")

    # --- account ---
    L.append("  ACCOUNT")
    L.append(f"    Starting balance      {_money(firm.starting_balance):>16}")
    L.append(f"    Final balance         {_money(acct.balance):>16}"
             f"   ({(acct.balance / firm.starting_balance - 1) * 100:+.2f}%)")
    L.append(f"    Profit target         {_money(firm.profit_target):>16}"
             f"   {_bar(min(acct.progress, 1.0) * 100)} {acct.progress * 100:5.1f}%")
    L.append(f"    Drawdown floor        {_money(acct.floor):>16}   ({firm.drawdown_model})")
    L.append(f"    Lowest equity         {_money(acct.min_equity):>16}"
             f"   closest approach to floor: {_money(acct.min_equity - acct.floor)}")
    L.append(f"    Peak equity           {_money(acct.peak_equity):>16}")
    L.append("")

    # --- trading ---
    L.append("  TRADING")
    L.append(f"    Trades                {m.trades:>16}"
             f"   ({m.trades / max(acct.trading_days, 1):.1f} per day)")
    L.append(f"    Win rate              {m.win_rate:>15.1f}%   {_bar(m.win_rate)}")
    L.append(f"    Profit factor         {m.profit_factor:>16.2f}"
             f"   {'(>1.3 is a real edge)' if m.profit_factor < 1.3 else ''}")
    L.append(f"    Expectancy            {m.expectancy_r:>15.3f}R   {_money(m.expectancy_dollars)} per trade")
    L.append(f"    Avg win / avg loss    {_money(m.avg_win):>16} / {_money(m.avg_loss)}"
             f"   payoff {m.payoff_ratio:.2f}")
    L.append(f"    Largest win / loss    {_money(m.largest_win):>16} / {_money(m.largest_loss)}")
    L.append(f"    Max consecutive loss  {m.max_consecutive_losses:>16}")
    L.append(f"    Max equity drawdown   {_money(m.max_drawdown):>16}   ({m.max_drawdown_pct:.2f}%)")
    L.append(f"    Sharpe (per trade)    {m.sharpe:>16.2f}")
    L.append("")

    # --- costs: the part backtests usually hide ---
    L.append("  COSTS")
    L.append(f"    Commission + fees     {_money(m.total_costs):>16}")
    gross = m.net_pnl + m.total_costs
    L.append(f"    Gross P&L             {_money(gross):>16}")
    L.append(f"    Net P&L               {_money(m.net_pnl):>16}")
    if abs(gross) > 1e-9:
        L.append(f"    Cost drag             {m.cost_drag_pct:>15.1f}%   of gross P&L consumed by costs")
    L.append("")

    if m.by_strategy:
        L.append("  BY STRATEGY")
        L.append(f"    {'strategy':<22}{'trades':>8}{'win%':>8}{'exp R':>9}{'net':>14}")
        for name, s in sorted(m.by_strategy.items(), key=lambda kv: -kv[1]["net"]):
            L.append(f"    {name:<22}{s['trades']:>8}{s['win_rate']:>8.1f}"
                     f"{s['expectancy_r']:>9.3f}{_money(s['net']):>14}")
        L.append("")

    if m.by_symbol:
        L.append("  BY SYMBOL")
        L.append(f"    {'symbol':<22}{'trades':>8}{'win%':>8}{'exp R':>9}{'net':>14}")
        for name, s in sorted(m.by_symbol.items(), key=lambda kv: -kv[1]["net"]):
            L.append(f"    {name:<22}{s['trades']:>8}{s['win_rate']:>8.1f}"
                     f"{s['expectancy_r']:>9.3f}{_money(s['net']):>14}")
        L.append("")

    if m.by_exit_reason:
        L.append("  EXITS")
        total = sum(m.by_exit_reason.values())
        for reason, count in sorted(m.by_exit_reason.items(), key=lambda kv: -kv[1]):
            L.append(f"    {reason:<22}{count:>8}   {100 * count / total:5.1f}%")
        L.append("")

    # --- what the guard blocked ---
    if result.rejections:
        L.append("  RISK GUARD — signals blocked")
        L.append(f"    {result.signals_generated} signals generated, "
                 f"{result.signals_taken} taken "
                 f"({100 * result.signals_taken / max(result.signals_generated, 1):.1f}%)")
        for reason, count in sorted(result.rejections.items(), key=lambda kv: -kv[1]):
            L.append(f"    {reason:<40}{count:>8}")
        L.append("")

    L.append(f"  VERDICT: {m.edge_verdict}")
    L.append("=" * 78)
    return "\n".join(L)


def monte_carlo_report(stats: EvaluationStats, config: BotConfig, seeds: int) -> str:
    firm = config.firm
    L: List[str] = []
    L.append("=" * 78)
    L.append(f"  EVALUATION SIMULATION — {firm.display_name}")
    L.append("=" * 78)
    L.append(f"  {seeds} independent runs, each a fresh evaluation attempt")
    L.append("")
    L.append(f"    Passed        {stats.passed:>6}   {_bar(stats.pass_rate)} {stats.pass_rate:5.1f}%")
    L.append(f"    Blown         {stats.failed:>6}   {_bar(stats.fail_rate)} {stats.fail_rate:5.1f}%")
    incomplete_pct = 100 - stats.pass_rate - stats.fail_rate
    L.append(f"    Ran out       {stats.incomplete:>6}   {_bar(incomplete_pct)} {incomplete_pct:5.1f}%")
    L.append("")
    L.append(f"    95% confidence interval on pass rate: "
             f"{stats.pass_ci_low:.1f}% – {stats.pass_ci_high:.1f}%")
    if stats.median_days_to_pass is not None:
        L.append(f"    Median trading days to pass: {stats.median_days_to_pass:.0f}")
    L.append("")
    L.append(f"    Final balance   avg {_money(stats.avg_final_balance)}"
             f"   worst {_money(stats.worst_final_balance)}"
             f"   best {_money(stats.best_final_balance)}")
    L.append(f"    Avg max drawdown  {_money(stats.avg_max_drawdown)}")
    L.append("")
    L.append("  READ THIS BEFORE QUOTING THE PASS RATE")
    L.append("    This number describes the DATA IT WAS RUN ON and nothing else.")
    L.append("    Run against synthetic bars it measures whether the risk engine")
    L.append("    holds the line — not whether the strategy has an edge. A pass")
    L.append("    rate from synthetic data is a plumbing test, not a forecast.")
    L.append("    Re-run it on your own historical bars before it means anything.")
    L.append("=" * 78)
    return "\n".join(L)


def trade_log(trades: Sequence[Trade], limit: int = 40) -> str:
    L = [f"{'entry':<17}{'sym':<9}{'side':<6}{'strategy':<20}{'size':>8}{'R':>7}{'net':>12}{'exit':>14}"]
    L.append("-" * 93)
    for t in trades[:limit]:
        L.append(
            f"{t.entry_ts:%Y-%m-%d %H:%M}  {t.symbol:<9}{t.side.value:<6}{t.strategy:<20}"
            f"{t.size:>8.3f}{t.r_multiple:>7.2f}{_money(t.net_pnl):>12}{t.exit_reason.value:>14}"
        )
    if len(trades) > limit:
        L.append(f"... {len(trades) - limit} more trades")
    return "\n".join(L)
