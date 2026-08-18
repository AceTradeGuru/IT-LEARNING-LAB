"""Position lifecycle: opening, managing stops, and closing.

Exit precedence within a bar is the subtle part. When a single bar's range
covers both the stop and the target, this engine assumes the STOP filled first.
That is not always true, but assuming the target filled first is how backtests
manufacture fictional win rates, and on a 1-minute scalper with a 1.5R target
the difference is enormous. Pessimism here is the only honest default without
tick data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ..config import BotConfig, Instrument
from ..data.types import Bar, ExitReason, Position, Side, Signal, Trade
from .broker_sim import FillModel, costs_for


@dataclass
class Portfolio:
    """Holds open positions and turns closed ones into `Trade` records."""

    config: BotConfig
    fills: FillModel = field(default_factory=FillModel)
    positions: Dict[str, Position] = field(default_factory=dict)
    closed: List[Trade] = field(default_factory=list)

    # ------------------------------------------------------------------
    # opening
    # ------------------------------------------------------------------

    def open_position(self, signal: Signal, size: float, bar: Bar) -> Position:
        instrument = self.config.instruments[signal.symbol]
        fill = self.fills.entry_fill(instrument, signal.side, signal.entry_price, signal.atr)

        # The stop stays where the strategy put it, so entry slippage eats into
        # the reward rather than silently widening the risk we already sized for.
        risk_per_unit = abs(fill - signal.stop_price)
        pos = Position(
            symbol=signal.symbol,
            side=signal.side,
            size=size,
            entry_price=fill,
            entry_ts=bar.ts,
            stop_price=signal.stop_price,
            target_price=signal.target_price,
            strategy=signal.strategy,
            atr_at_entry=signal.atr,
            initial_risk_per_unit=risk_per_unit if risk_per_unit > 0 else signal.stop_distance,
            meta={"signal_reason": signal.reason, "signal_price": signal.entry_price},
        )
        self.positions[self._key(signal.symbol, signal.strategy)] = pos
        return pos

    @staticmethod
    def _key(symbol: str, strategy: str) -> str:
        return f"{symbol}:{strategy}"

    # ------------------------------------------------------------------
    # marking
    # ------------------------------------------------------------------

    def unrealized(self, prices: Dict[str, float]) -> float:
        total = 0.0
        for pos in self.positions.values():
            price = prices.get(pos.symbol)
            if price is None:
                continue
            instrument = self.config.instruments[pos.symbol]
            total += pos.unrealized(price, instrument.value_per_point)
        return total

    def worst_case_unrealized(self, prices: Dict[str, float]) -> float:
        """Unrealized PnL if every open position stopped out right now.

        The intraday-trailing firms measure drawdown against peak equity, so
        knowing the floor of the current bar matters as much as its close.
        """
        total = 0.0
        for pos in self.positions.values():
            instrument = self.config.instruments[pos.symbol]
            total += (pos.stop_price - pos.entry_price) * pos.side.sign * pos.size * instrument.value_per_point
        return total

    # ------------------------------------------------------------------
    # management
    # ------------------------------------------------------------------

    def update_stops(self, pos: Position, bar: Bar, params) -> None:
        """Breakeven and trailing logic, applied on the bar's close.

        Both only ever move the stop in the favourable direction — a stop that
        can loosen is not a stop.
        """
        r = pos.r_multiple(bar.close)
        pos.max_favorable = max(pos.max_favorable, r)
        pos.max_adverse = min(pos.max_adverse, r)

        be_at = params.getf("breakeven_at_r", 0.0)
        if be_at > 0 and not pos.moved_to_breakeven and r >= be_at:
            # Breakeven plus the round-turn cost, so a "scratch" is truly flat.
            instrument = self.config.instruments[pos.symbol]
            cost_pad = (costs_for(instrument, pos.size, pos.entry_price) / max(pos.size, 1e-9)) / max(
                instrument.value_per_point, 1e-9
            )
            new_stop = pos.entry_price + pos.side.sign * cost_pad
            if self._improves(pos, new_stop):
                pos.stop_price = instrument.round_price(new_stop)
                pos.moved_to_breakeven = True

        trail_after = params.getf("trail_after_r", 0.0)
        trail_mult = params.getf("trail_atr_mult", 0.0)
        if trail_mult > 0 and trail_after > 0 and r >= trail_after:
            pos.trailing_active = True
        if pos.trailing_active and trail_mult > 0:
            instrument = self.config.instruments[pos.symbol]
            anchor = bar.high if pos.side is Side.LONG else bar.low
            new_stop = anchor - pos.side.sign * trail_mult * pos.atr_at_entry
            if self._improves(pos, new_stop):
                pos.stop_price = instrument.round_price(new_stop)

    @staticmethod
    def _improves(pos: Position, new_stop: float) -> bool:
        return (new_stop - pos.stop_price) * pos.side.sign > 0

    # ------------------------------------------------------------------
    # closing
    # ------------------------------------------------------------------

    def check_exit(self, pos: Position, bar: Bar, params) -> Optional[ExitReason]:
        """Decide whether `bar` closes this position, and why.

        Stop is checked before target: see the module docstring.
        """
        if pos.side is Side.LONG:
            if bar.low <= pos.stop_price:
                return ExitReason.TRAIL if pos.trailing_active or pos.moved_to_breakeven else ExitReason.STOP
            if bar.high >= pos.target_price:
                return ExitReason.TARGET
        else:
            if bar.high >= pos.stop_price:
                return ExitReason.TRAIL if pos.trailing_active or pos.moved_to_breakeven else ExitReason.STOP
            if bar.low <= pos.target_price:
                return ExitReason.TARGET

        time_stop = params.geti("time_stop_bars", 0)
        if time_stop and pos.bars_held >= time_stop:
            return ExitReason.TIME_STOP
        return None

    def close_position(
        self,
        pos: Position,
        bar: Bar,
        reason: ExitReason,
    ) -> Trade:
        instrument = self.config.instruments[pos.symbol]
        intended = pos.stop_price if reason in (ExitReason.STOP, ExitReason.TRAIL) else (
            pos.target_price if reason is ExitReason.TARGET else bar.close
        )
        exit_price = self.fills.exit_fill(instrument, pos.side, intended, bar, pos.atr_at_entry, reason)

        gross = (exit_price - pos.entry_price) * pos.side.sign * pos.size * instrument.value_per_point
        costs = costs_for(instrument, pos.size, pos.entry_price)
        net = gross - costs
        risk_dollars = pos.initial_risk_per_unit * pos.size * instrument.value_per_point
        r_mult = net / risk_dollars if risk_dollars > 0 else 0.0

        trade = Trade(
            symbol=pos.symbol,
            side=pos.side,
            strategy=pos.strategy,
            size=pos.size,
            entry_ts=pos.entry_ts,
            entry_price=pos.entry_price,
            exit_ts=bar.ts,
            exit_price=exit_price,
            gross_pnl=gross,
            costs=costs,
            net_pnl=net,
            r_multiple=r_mult,
            exit_reason=reason,
            bars_held=pos.bars_held,
            max_favorable_r=pos.max_favorable,
            max_adverse_r=pos.max_adverse,
        )
        self.closed.append(trade)
        self.positions.pop(self._key(pos.symbol, pos.strategy), None)
        return trade

    def close_all(self, bars: Dict[str, Bar], reason: ExitReason) -> List[Trade]:
        out = []
        for pos in list(self.positions.values()):
            bar = bars.get(pos.symbol)
            if bar is not None:
                out.append(self.close_position(pos, bar, reason))
        return out

    def positions_for(self, symbol: str) -> List[Position]:
        return [p for p in self.positions.values() if p.symbol == symbol]
