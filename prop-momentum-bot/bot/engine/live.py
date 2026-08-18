"""Paper / live trading loop.

Runs the identical decision sequence as the backtest — mark, check rules,
manage, flatten, signal, guard — but against a `BrokerAdapter` instead of a
list of bars. The point of the shared sequence is that a paper run is
comparable evidence: if paper diverges from backtest on the same data, the
difference is real, not an artefact of two different engines.

Reconciliation: on every cycle the loop compares its own view of open positions
against the broker's. A mismatch is logged loudly and, by default, halts new
entries — an engine that has lost track of what it owns must not size a new
trade off a stale equity figure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

from ..config import BotConfig
from ..data.types import Bar, ExitReason, Position, Side, Trade
from ..risk.account import Account
from ..risk.guard import RiskGuard
from ..signals import IndicatorCache, make_strategy
from ..brokers.base import BrokerAdapter, BrokerError, Order, OrderType
from .broker_sim import FillModel
from .portfolio import Portfolio

log = logging.getLogger("bot.live")

# How much history each strategy needs before its indicators are valid.
WARMUP_BARS = 200


@dataclass
class LiveState:
    cycles: int = 0
    orders_sent: int = 0
    rejections: Dict[str, int] = field(default_factory=dict)
    reconcile_errors: int = 0
    halted: bool = False
    halt_reason: str = ""


class LiveTrader:
    """Drives one account against one broker."""

    def __init__(
        self,
        config: BotConfig,
        broker: BrokerAdapter,
        symbols: Sequence[str],
        strategies: Optional[Sequence[str]] = None,
        halt_on_reconcile_error: bool = True,
    ) -> None:
        self.config = config
        self.broker = broker
        self.symbols = [s for s in symbols if s in config.instruments_for_firm()]
        if not self.symbols:
            raise ValueError(
                f"no symbols from {list(symbols)} are permitted by firm {config.firm.key}"
            )
        self.account = Account(config.firm)
        self.guard = RiskGuard(config, self.account)
        self.portfolio = Portfolio(config, FillModel())
        self.state = LiveState()
        self.halt_on_reconcile_error = halt_on_reconcile_error

        names = list(strategies) if strategies else [n for n, p in config.strategies.items() if p.enabled]
        self.strategy_names = names
        self.history: Dict[str, List[Bar]] = {s: [] for s in self.symbols}

    # ------------------------------------------------------------------

    def warmup(self) -> None:
        """Pull enough history for indicators to leave their NaN window."""
        for symbol in self.symbols:
            try:
                bars = self.broker.latest_bars(symbol, 1, WARMUP_BARS)
            except (BrokerError, NotImplementedError) as exc:
                log.warning("warmup failed for %s: %s", symbol, exc)
                continue
            self.history[symbol] = list(bars)
            log.info("warmed up %s with %d bars", symbol, len(bars))

    def on_bar(self, bar: Bar) -> None:
        """Feed one closed bar. This is the loop's only entry point.

        Only CLOSED bars may be passed in. Acting on a forming bar is the
        live-trading equivalent of lookahead bias and will not reproduce.
        """
        symbol = bar.symbol
        if symbol not in self.history:
            return
        self.history[symbol].append(bar)
        if len(self.history[symbol]) > 5000:
            self.history[symbol] = self.history[symbol][-3000:]

        self.state.cycles += 1
        ts = bar.ts
        last_prices = {s: h[-1].close for s, h in self.history.items() if h}
        last_bars = {s: h[-1] for s, h in self.history.items() if h}

        # --- 1 & 2: mark and check the firm's rules ---------------------
        status = self.account.mark(ts, self.portfolio.unrealized(last_prices))
        if status.failed:
            self._halt(f"evaluation failed: {self.account.failed_reason}", last_bars)
            return
        if status.passed and not self.state.halted:
            log.info("PASSED: %s", status.reason)

        # --- reconciliation --------------------------------------------
        if not self._reconcile():
            if self.halt_on_reconcile_error:
                self.state.halted = True
                self.state.halt_reason = "position reconciliation mismatch"
                return

        # --- 3: manage open positions ----------------------------------
        for pos in list(self.portfolio.positions.values()):
            if pos.symbol != symbol:
                continue
            pos.bars_held += 1
            params = self.config.strategies[pos.strategy]
            reason = self.portfolio.check_exit(pos, bar, params)
            if reason:
                self._close(pos, bar, reason)
                continue
            self.portfolio.update_stops(pos, bar, params)

        # --- 4: session flatten ----------------------------------------
        for pos in list(self.portfolio.positions.values()):
            inst = self.config.instruments[pos.symbol]
            if self.guard.should_flatten(inst, ts):
                pbar = last_bars.get(pos.symbol)
                if pbar:
                    self._close(pos, pbar, ExitReason.SESSION_CLOSE)

        self.account.mark(ts, self.portfolio.unrealized(last_prices))

        # --- 5 & 6: signals through the guard ---------------------------
        for sname in self.strategy_names:
            params = self.config.strategies[sname]
            inst = self.config.instruments[symbol]
            bars = self._bars_for(symbol, params.timeframe_minutes)
            if len(bars) < 60:
                continue
            strat = make_strategy(sname, params, inst)
            cache = strat.build_cache(bars)
            signal = strat.on_bar(cache, len(bars) - 1)
            if signal is None:
                continue

            decision = self.guard.evaluate(signal, self.portfolio.positions, ts)
            if not decision.approved:
                self.state.rejections[decision.reason[:48]] = (
                    self.state.rejections.get(decision.reason[:48], 0) + 1
                )
                log.debug("signal rejected: %s", decision.reason)
                continue
            self._open(signal, decision.size, bar)

    # ------------------------------------------------------------------

    def _bars_for(self, symbol: str, timeframe_minutes: int) -> List[Bar]:
        from ..data.feed import resample

        base = self.history[symbol]
        if timeframe_minutes <= 1:
            return base
        bars = resample(base, timeframe_minutes)
        # Drop the final bucket: it is still forming and acting on it would be
        # exactly the lookahead the engine is built to avoid.
        return bars[:-1] if bars else bars

    def _open(self, signal, size: float, bar: Bar) -> None:
        order = Order(
            symbol=signal.symbol,
            side=signal.side,
            size=size,
            order_type=OrderType.MARKET,
            client_id=f"{signal.strategy}-{int(bar.ts.timestamp())}",
        )
        try:
            filled = self.broker.submit(order)
        except BrokerError as exc:
            log.error("order failed for %s: %s", signal.symbol, exc)
            self.state.rejections["broker_error"] = self.state.rejections.get("broker_error", 0) + 1
            return
        self.state.orders_sent += 1
        if filled.filled_price:
            signal.entry_price = filled.filled_price
        pos = self.portfolio.open_position(signal, filled.filled_size or size, bar)
        log.info(
            "OPEN %s %s %.4f @ %.5f stop %.5f target %.5f (%s)",
            pos.side.value, pos.symbol, pos.size, pos.entry_price,
            pos.stop_price, pos.target_price, signal.reason,
        )

    def _close(self, pos: Position, bar: Bar, reason: ExitReason) -> None:
        order = Order(
            symbol=pos.symbol,
            side=pos.side.opposite,
            size=pos.size,
            order_type=OrderType.MARKET,
            client_id=f"exit-{pos.symbol}-{int(bar.ts.timestamp())}",
        )
        try:
            self.broker.submit(order)
        except BrokerError as exc:
            # A failed exit is the dangerous case: the position is still live
            # and the engine must not pretend otherwise.
            log.error("EXIT FAILED for %s: %s — position still open at broker", pos.symbol, exc)
            self.state.reconcile_errors += 1
            return
        trade = self.portfolio.close_position(pos, bar, reason)
        self.account.on_trade_closed(trade)
        log.info(
            "CLOSE %s %s @ %.5f %s net $%.2f (%.2fR)",
            trade.side.value, trade.symbol, trade.exit_price,
            reason.value, trade.net_pnl, trade.r_multiple,
        )

    def _halt(self, reason: str, last_bars: Dict[str, Bar]) -> None:
        if self.state.halted:
            return
        self.state.halted = True
        self.state.halt_reason = reason
        log.critical("HALT: %s — flattening all positions", reason)
        for pos in list(self.portfolio.positions.values()):
            bar = last_bars.get(pos.symbol)
            if bar:
                self._close(pos, bar, ExitReason.RISK_HALT)

    def _reconcile(self) -> bool:
        """Compare internal positions with the broker's. True when they agree."""
        try:
            broker_positions = self.broker.positions()
        except BrokerError as exc:
            log.error("reconciliation failed: %s", exc)
            self.state.reconcile_errors += 1
            return False

        mine = {p.symbol: p for p in self.portfolio.positions.values()}
        ok = True
        for symbol, bp in broker_positions.items():
            mp = mine.get(symbol)
            if mp is None:
                log.error("broker holds %s %.4f that the engine does not know about", symbol, bp.size)
                ok = False
            elif abs(mp.size - bp.size) > 1e-8 or mp.side is not bp.side:
                log.error(
                    "size/side mismatch on %s: engine %s %.4f vs broker %s %.4f",
                    symbol, mp.side.value, mp.size, bp.side.value, bp.size,
                )
                ok = False
        for symbol, mp in mine.items():
            if symbol not in broker_positions:
                log.error("engine holds %s %.4f that the broker does not report", symbol, mp.size)
                ok = False
        if not ok:
            self.state.reconcile_errors += 1
        return ok

    def status_line(self) -> str:
        return (
            f"[{self.config.firm.key}] {self.account.describe()} | "
            f"open {len(self.portfolio.positions)} | trades {len(self.portfolio.closed)} | "
            f"{'HALTED: ' + self.state.halt_reason if self.state.halted else 'running'}"
        )
