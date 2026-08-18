"""Paper broker: fills against the bar feed, holds no real money.

This is the only fully implemented adapter, and it is what the paper-trading
loop runs against. It reuses `FillModel`, so paper fills carry the same spread
and slippage assumptions as the backtest — a paper run that looks different
from the backtest is telling you about the strategy, not about the fill model.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

from ..config import Instrument
from ..data.types import Bar, Side
from ..engine.broker_sim import FillModel
from .base import BrokerAdapter, BrokerPosition, Order, OrderStatus, OrderType


class PaperBroker(BrokerAdapter):
    name = "paper"
    supports_live = False

    def __init__(
        self,
        instruments: Dict[str, Instrument],
        starting_balance: float,
        fills: Optional[FillModel] = None,
    ) -> None:
        super().__init__(instruments, dry_run=True)
        self.balance = starting_balance
        self.fills = fills or FillModel()
        self._positions: Dict[str, BrokerPosition] = {}
        self._bars: Dict[str, List[Bar]] = {}
        self._last_price: Dict[str, float] = {}
        self._atr: Dict[str, float] = {}
        self._order_seq = 0

    # -- lifecycle ------------------------------------------------------

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    # -- feed -----------------------------------------------------------

    def push_bar(self, bar: Bar, atr: float = 0.0) -> None:
        """Feed the broker a new bar. Drives fills and mark-to-market."""
        self._bars.setdefault(bar.symbol, []).append(bar)
        self._last_price[bar.symbol] = bar.close
        if atr:
            self._atr[bar.symbol] = atr
        pos = self._positions.get(bar.symbol)
        if pos:
            inst = self.instruments[bar.symbol]
            pos.unrealized_pnl = (
                (bar.close - pos.entry_price) * pos.side.sign * pos.size * inst.value_per_point
            )

    def latest_bars(self, symbol: str, timeframe_minutes: int, count: int) -> List[Bar]:
        return self._bars.get(symbol, [])[-count:]

    # -- account --------------------------------------------------------

    def account_balance(self) -> float:
        return self.balance

    def equity(self) -> float:
        return self.balance + sum(p.unrealized_pnl for p in self._positions.values())

    def positions(self) -> Dict[str, BrokerPosition]:
        return dict(self._positions)

    # -- orders ---------------------------------------------------------

    def submit(self, order: Order) -> Order:
        self._require_connected()
        self._order_seq += 1
        order.broker_id = f"paper-{self._order_seq}"
        order.submitted_at = datetime.now(timezone.utc)

        price = self._last_price.get(order.symbol)
        if price is None:
            order.status = OrderStatus.REJECTED
            order.message = f"no market data for {order.symbol}"
            return order

        inst = self.instruments[order.symbol]
        atr = self._atr.get(order.symbol, inst.typical_atr_points * 0.05)
        fill = self.fills.entry_fill(inst, order.side, price, atr)

        existing = self._positions.get(order.symbol)
        if existing and existing.side is not order.side:
            # Closing (or reducing) an existing position realises PnL.
            closing = min(existing.size, order.size)
            realized = (
                (fill - existing.entry_price) * existing.side.sign * closing * inst.value_per_point
            )
            self.balance += realized - inst.cost_for(closing, existing.entry_price)
            remaining = existing.size - closing
            if remaining <= 0:
                self._positions.pop(order.symbol, None)
                opening = order.size - closing
                if opening > 0:
                    self._positions[order.symbol] = BrokerPosition(
                        order.symbol, order.side, opening, fill
                    )
            else:
                existing.size = remaining
        elif existing:
            # Adding to a position: recompute the weighted average entry.
            total = existing.size + order.size
            existing.entry_price = (
                existing.entry_price * existing.size + fill * order.size
            ) / total
            existing.size = total
        else:
            self._positions[order.symbol] = BrokerPosition(order.symbol, order.side, order.size, fill)

        order.status = OrderStatus.FILLED
        order.filled_price = fill
        order.filled_size = order.size
        return order

    def cancel(self, order: Order) -> None:
        order.status = OrderStatus.CANCELLED
