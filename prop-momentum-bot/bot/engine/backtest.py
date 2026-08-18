"""Event-driven backtest.

The loop walks bars in timestamp order across every symbol and strategy, and
at each bar does exactly what the live loop does, in the same order:

    1. mark open positions and update the account (equity, high-water marks)
    2. check firm rules — a breach halts everything immediately
    3. manage open positions (trail stops, check exits)
    4. flatten if the session is closing and overnight holds are forbidden
    5. ask strategies for new signals
    6. run each signal through the risk guard, which sizes or vetoes it

Step 2 before step 5 is deliberate: a bar that breaches the drawdown floor must
not also be allowed to open a new trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from ..config import BotConfig
from ..data.feed import align_feeds, resample
from ..data.types import Bar, ExitReason, RiskDecision, Signal, Trade
from ..risk.account import Account
from ..risk.guard import RiskGuard, session_window
from ..signals import IndicatorCache, make_strategy
from .broker_sim import FillModel
from .portfolio import Portfolio


@dataclass
class EquityPoint:
    ts: datetime
    equity: float
    balance: float
    floor: float
    drawdown: float


@dataclass
class BacktestResult:
    firm_key: str
    trades: List[Trade]
    equity_curve: List[EquityPoint]
    account: Account
    rejections: Dict[str, int] = field(default_factory=dict)
    signals_generated: int = 0
    signals_taken: int = 0
    start_ts: Optional[datetime] = None
    end_ts: Optional[datetime] = None

    @property
    def passed(self) -> bool:
        return self.account.passed

    @property
    def failed(self) -> bool:
        return self.account.status.failed

    @property
    def outcome(self) -> str:
        if self.failed:
            return "FAILED"
        if self.passed:
            return "PASSED"
        return "INCOMPLETE"


class Backtester:
    """Runs one evaluation attempt over a fixed set of bars."""

    def __init__(
        self,
        config: BotConfig,
        fills: Optional[FillModel] = None,
        strategies: Optional[Sequence[str]] = None,
    ) -> None:
        self.config = config
        self.fills = fills or FillModel()
        enabled = [n for n, p in config.strategies.items() if p.enabled]
        self.strategy_names = list(strategies) if strategies else enabled

    def run(
        self,
        data: Dict[str, List[Bar]],
        news_events: Optional[Sequence[datetime]] = None,
    ) -> BacktestResult:
        """`data` maps symbol -> 1-minute base bars. Coarser timeframes are
        resampled internally so every strategy sees its own bar clock."""
        cfg = self.config
        account = Account(cfg.firm)
        guard = RiskGuard(cfg, account)
        if news_events:
            guard.load_news_blackouts(news_events)
        portfolio = Portfolio(cfg, self.fills)

        symbols = [s for s in data if s in cfg.instruments_for_firm()]
        if not symbols:
            raise ValueError(
                f"none of {sorted(data)} are permitted by firm profile "
                f"{cfg.firm.key} (allows {cfg.firm.asset_classes})"
            )

        # Build per-(symbol, strategy) caches on that strategy's timeframe.
        contexts: Dict[Tuple[str, str], dict] = {}
        for symbol in symbols:
            instrument = cfg.instruments[symbol]
            base = data[symbol]
            for sname in self.strategy_names:
                params = cfg.strategies[sname]
                bars = resample(base, params.timeframe_minutes) if params.timeframe_minutes > 1 else base
                strat = make_strategy(sname, params, instrument)
                contexts[(symbol, sname)] = {
                    "strategy": strat,
                    "params": params,
                    "bars": bars,
                    "cache": strat.build_cache(bars),
                    "index": {b.ts: k for k, b in enumerate(bars)},
                }

        result = BacktestResult(firm_key=cfg.firm.key, trades=[], equity_curve=[], account=account)
        last_prices: Dict[str, float] = {}
        last_bars: Dict[str, Bar] = {}

        for ts, group in align_feeds({s: data[s] for s in symbols}):
            for symbol, bar in group.items():
                last_prices[symbol] = bar.close
                last_bars[symbol] = bar

            # --- 1 & 2: mark, then check the firm's rules -------------
            unrealized = portfolio.unrealized(last_prices)
            status = account.mark(ts, unrealized)
            floor = account.floor
            result.equity_curve.append(
                EquityPoint(ts, account.equity, account.balance, floor, account.equity - floor)
            )
            if result.start_ts is None:
                result.start_ts = ts

            if status.failed:
                # Blown accounts stop trading. Flatten and exit the loop.
                for t in portfolio.close_all(last_bars, ExitReason.RISK_HALT):
                    account.on_trade_closed(t)
                    result.trades.append(t)
                account.mark(ts, 0.0)
                result.end_ts = ts
                break

            # --- 3: manage open positions -----------------------------
            for pos in list(portfolio.positions.values()):
                bar = group.get(pos.symbol)
                if bar is None:
                    continue
                pos.bars_held += 1
                params = cfg.strategies[pos.strategy]

                # Exit is checked against the stop that was already in force
                # when this bar opened, and only THEN is the stop moved for the
                # next bar. Doing it the other way round lets a stop moved on
                # this bar's close be hit by this bar's low — price action that
                # happened before the stop existed. That single ordering error
                # turns a breakeven stop into a guaranteed loss generator.
                reason = portfolio.check_exit(pos, bar, params)
                if reason:
                    trade = portfolio.close_position(pos, bar, reason)
                    account.on_trade_closed(trade)
                    result.trades.append(trade)
                    continue
                portfolio.update_stops(pos, bar, params)

            # --- 4: session flatten -----------------------------------
            for pos in list(portfolio.positions.values()):
                bar = group.get(pos.symbol)
                if bar is None:
                    continue
                if guard.should_flatten(cfg.instruments[pos.symbol], ts):
                    trade = portfolio.close_position(pos, bar, ExitReason.SESSION_CLOSE)
                    account.on_trade_closed(trade)
                    result.trades.append(trade)

            account.mark(ts, portfolio.unrealized(last_prices))

            # --- 5 & 6: new signals through the guard -----------------
            for (symbol, sname), ctx in contexts.items():
                if symbol not in group:
                    continue
                idx = ctx["index"].get(group[symbol].ts)
                if idx is None:
                    continue  # this symbol's coarse bar has not closed yet
                signal = ctx["strategy"].on_bar(ctx["cache"], idx)
                if signal is None:
                    continue
                result.signals_generated += 1

                decision = guard.evaluate(signal, portfolio.positions, ts)
                if not decision.approved:
                    key = _rejection_key(decision.reason)
                    result.rejections[key] = result.rejections.get(key, 0) + 1
                    continue
                portfolio.open_position(signal, decision.size, group[symbol])
                result.signals_taken += 1

            result.end_ts = ts

        # Settle anything still open at the end of the data.
        for t in portfolio.close_all(last_bars, ExitReason.SESSION_CLOSE):
            account.on_trade_closed(t)
            result.trades.append(t)
        if result.end_ts:
            account.mark(result.end_ts, 0.0)
        account.finalize()
        return result


def _rejection_key(reason: str) -> str:
    """Collapse rejection reasons into countable buckets for the report."""
    lowered = reason.lower()
    for marker in (
        "halted", "outside", "session close", "max concurrent", "already in",
        "correlation", "worst case", "no risk budget", "below the", "stop",
        "weekend", "news", "not permitted", "round-turn cost",
    ):
        if marker in lowered:
            return marker
    return reason[:40]
