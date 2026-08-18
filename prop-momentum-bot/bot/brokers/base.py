"""Broker adapter interface.

Every venue — crypto exchange, FX broker, futures gateway — is reduced to this
one interface so the engine never contains venue-specific logic. Adding a new
broker means implementing this class and nothing else.

Safety posture: all live adapters ship DISABLED. Each raises on connect unless
explicitly enabled in code AND given credentials from the environment. There is
no code path in this repository that places a real order without someone
deliberately turning it on.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from ..config import Instrument
from ..data.types import Bar, Side


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderStatus(str, Enum):
    PENDING = "pending"
    FILLED = "filled"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


@dataclass
class Order:
    symbol: str
    side: Side
    size: float
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None
    client_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    filled_price: Optional[float] = None
    filled_size: float = 0.0
    broker_id: Optional[str] = None
    message: str = ""
    submitted_at: Optional[datetime] = None


@dataclass
class BrokerPosition:
    symbol: str
    side: Side
    size: float
    entry_price: float
    unrealized_pnl: float = 0.0


class BrokerError(RuntimeError):
    """Raised for any venue-side failure. Callers must treat this as
    'order state unknown' and reconcile, never as 'order did not happen'."""


class BrokerAdapter(ABC):
    """One venue. Implementations must be idempotent on reconnect."""

    name: str = "broker"
    supports_live: bool = False

    def __init__(self, instruments: Dict[str, Instrument], dry_run: bool = True) -> None:
        self.instruments = instruments
        self.dry_run = dry_run
        self.connected = False

    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def account_balance(self) -> float:
        ...

    @abstractmethod
    def positions(self) -> Dict[str, BrokerPosition]:
        ...

    @abstractmethod
    def submit(self, order: Order) -> Order:
        ...

    @abstractmethod
    def cancel(self, order: Order) -> None:
        ...

    @abstractmethod
    def latest_bars(self, symbol: str, timeframe_minutes: int, count: int) -> List[Bar]:
        ...

    def flatten(self, symbol: str) -> Optional[Order]:
        """Close any open position in `symbol` at market."""
        pos = self.positions().get(symbol)
        if not pos or pos.size == 0:
            return None
        return self.submit(
            Order(
                symbol=symbol,
                side=pos.side.opposite,
                size=abs(pos.size),
                order_type=OrderType.MARKET,
                client_id=f"flatten-{symbol}",
            )
        )

    def _require_connected(self) -> None:
        if not self.connected:
            raise BrokerError(f"{self.name}: not connected")
