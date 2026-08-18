"""Live venue adapters — WIRED BUT DISABLED.

Each class below has the full call structure for its venue and raises
`AdapterDisabled` on connect. Turning one on is a deliberate three-step act:

    1. install that venue's SDK (see requirements-live.txt)
    2. export the credentials the adapter names
    3. construct it with `enabled=True`

Nothing in the default code path constructs these, and no credential is ever
read from a config file — only from the environment — so a checked-in config
can never carry a key to a public repo.

WHAT IS AND IS NOT DONE HERE: the request/response shapes below follow each
venue's documented REST API, but they have not been executed against a live
endpoint from this repository. Treat every adapter as unverified until you have
run it against the venue's own demo/sandbox environment and reconciled fills by
hand. Do not point one at a funded evaluation account before that.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from ..config import Instrument
from ..data.types import Bar, Side
from .base import BrokerAdapter, BrokerError, BrokerPosition, Order, OrderStatus, OrderType


class AdapterDisabled(BrokerError):
    """Raised when a live adapter is used without being explicitly enabled."""


class _LiveAdapter(BrokerAdapter):
    """Shared plumbing: env-var credentials and the enable gate."""

    required_env: tuple[str, ...] = ()
    sdk_hint: str = ""

    def __init__(
        self,
        instruments: Dict[str, Instrument],
        enabled: bool = False,
        dry_run: bool = True,
        base_url: Optional[str] = None,
    ) -> None:
        super().__init__(instruments, dry_run=dry_run)
        self.enabled = enabled
        self.base_url = base_url
        self._session = None

    def _credentials(self) -> Dict[str, str]:
        missing = [k for k in self.required_env if not os.environ.get(k)]
        if missing:
            raise BrokerError(
                f"{self.name}: missing environment variables {missing}. "
                "Credentials are read from the environment only — never from config files."
            )
        return {k: os.environ[k] for k in self.required_env}

    def connect(self) -> None:
        if not self.enabled:
            raise AdapterDisabled(
                f"{self.name} adapter is disabled. This is the safety default. To enable: "
                f"install the SDK ({self.sdk_hint}), export {list(self.required_env)}, "
                f"and construct with enabled=True. Test against the venue's demo "
                f"environment before any funded account."
            )
        self._credentials()
        self._connect_impl()
        self.connected = True

    def _connect_impl(self) -> None:
        raise NotImplementedError

    def disconnect(self) -> None:
        self._session = None
        self.connected = False


class CcxtCryptoBroker(_LiveAdapter):
    """Crypto spot/perpetual venues via ccxt (Binance, Bybit, OKX, ...).

    Notes specific to crypto that the engine relies on:
      * `size` is denominated in the base coin, matching value_per_point = 1.0.
      * Perpetuals accrue funding every 8h; funding is NOT modelled in the
        backtest, so a strategy holding perps for hours will show slightly
        better backtest results than live. Intraday styles are barely affected.
      * There is no session close, so the flatten-before-close rule is inert.
    """

    name = "ccxt"
    supports_live = True
    required_env = ("CRYPTO_API_KEY", "CRYPTO_API_SECRET")
    sdk_hint = "pip install ccxt"

    def __init__(self, instruments, exchange_id: str = "binanceusdm", **kwargs) -> None:
        super().__init__(instruments, **kwargs)
        self.exchange_id = exchange_id

    def _connect_impl(self) -> None:
        try:
            import ccxt  # noqa: PLC0415 - optional dependency, imported on demand
        except ImportError as exc:
            raise BrokerError(f"{self.name}: {self.sdk_hint}") from exc
        creds = self._credentials()
        self._session = getattr(ccxt, self.exchange_id)(
            {
                "apiKey": creds["CRYPTO_API_KEY"],
                "secret": creds["CRYPTO_API_SECRET"],
                "enableRateLimit": True,
                "options": {"defaultType": "future"},
            }
        )
        if self.dry_run:
            self._session.set_sandbox_mode(True)

    def account_balance(self) -> float:
        self._require_connected()
        bal = self._session.fetch_balance()
        return float(bal["total"].get("USDT", 0.0))

    def positions(self) -> Dict[str, BrokerPosition]:
        self._require_connected()
        out: Dict[str, BrokerPosition] = {}
        for p in self._session.fetch_positions():
            size = float(p.get("contracts") or 0.0)
            if size == 0:
                continue
            symbol = p["symbol"].replace("/", "").split(":")[0]
            out[symbol] = BrokerPosition(
                symbol=symbol,
                side=Side.LONG if p.get("side") == "long" else Side.SHORT,
                size=abs(size),
                entry_price=float(p.get("entryPrice") or 0.0),
                unrealized_pnl=float(p.get("unrealizedPnl") or 0.0),
            )
        return out

    def submit(self, order: Order) -> Order:
        self._require_connected()
        market = self._to_market(order.symbol)
        side = "buy" if order.side is Side.LONG else "sell"
        try:
            resp = self._session.create_order(
                market,
                order.order_type.value,
                side,
                order.size,
                order.limit_price,
                {"reduceOnly": False},
            )
        except Exception as exc:  # noqa: BLE001 - venue errors are opaque
            raise BrokerError(f"{self.name}: order rejected: {exc}") from exc
        order.broker_id = str(resp.get("id"))
        order.status = OrderStatus.FILLED if resp.get("status") == "closed" else OrderStatus.PENDING
        order.filled_price = float(resp.get("average") or resp.get("price") or 0.0)
        order.filled_size = float(resp.get("filled") or 0.0)
        return order

    def cancel(self, order: Order) -> None:
        self._require_connected()
        if order.broker_id:
            self._session.cancel_order(order.broker_id, self._to_market(order.symbol))
        order.status = OrderStatus.CANCELLED

    def latest_bars(self, symbol: str, timeframe_minutes: int, count: int) -> List[Bar]:
        self._require_connected()
        tf = f"{timeframe_minutes}m" if timeframe_minutes < 60 else f"{timeframe_minutes // 60}h"
        raw = self._session.fetch_ohlcv(self._to_market(symbol), tf, limit=count)
        return [
            Bar(
                ts=datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc),
                open=float(r[1]), high=float(r[2]), low=float(r[3]),
                close=float(r[4]), volume=float(r[5]), symbol=symbol,
            )
            for r in raw
        ]

    @staticmethod
    def _to_market(symbol: str) -> str:
        for quote in ("USDT", "USDC", "USD"):
            if symbol.endswith(quote):
                return f"{symbol[: -len(quote)]}/{quote}"
        return symbol


class OandaFxBroker(_LiveAdapter):
    """FX via the OANDA v20 REST API.

    FX-specific notes:
      * OANDA sizes in UNITS, not lots. The engine sizes in lots, so this
        adapter multiplies by 100,000 on the way out — matching the
        value_per_point convention in instruments.yaml.
      * A short is a negative unit count, not a side flag.
    """

    name = "oanda"
    supports_live = True
    required_env = ("OANDA_API_TOKEN", "OANDA_ACCOUNT_ID")
    sdk_hint = "pip install requests"
    UNITS_PER_LOT = 100_000

    PRACTICE_URL = "https://api-fxpractice.oanda.com/v3"
    LIVE_URL = "https://api-fxtrade.oanda.com/v3"

    def _connect_impl(self) -> None:
        try:
            import requests  # noqa: PLC0415
        except ImportError as exc:
            raise BrokerError(f"{self.name}: {self.sdk_hint}") from exc
        creds = self._credentials()
        self.account_id = creds["OANDA_ACCOUNT_ID"]
        self.base_url = self.base_url or (self.PRACTICE_URL if self.dry_run else self.LIVE_URL)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {creds['OANDA_API_TOKEN']}",
                "Content-Type": "application/json",
            }
        )

    def _get(self, path: str, **params):
        resp = self._session.get(f"{self.base_url}{path}", params=params, timeout=15)
        if resp.status_code >= 400:
            raise BrokerError(f"{self.name}: GET {path} -> {resp.status_code} {resp.text[:200]}")
        return resp.json()

    def account_balance(self) -> float:
        self._require_connected()
        return float(self._get(f"/accounts/{self.account_id}")["account"]["balance"])

    def positions(self) -> Dict[str, BrokerPosition]:
        self._require_connected()
        data = self._get(f"/accounts/{self.account_id}/openPositions")
        out: Dict[str, BrokerPosition] = {}
        for p in data.get("positions", []):
            symbol = p["instrument"].replace("_", "")
            long_units = float(p["long"]["units"])
            short_units = float(p["short"]["units"])
            units = long_units + short_units
            if units == 0:
                continue
            leg = p["long"] if units > 0 else p["short"]
            out[symbol] = BrokerPosition(
                symbol=symbol,
                side=Side.LONG if units > 0 else Side.SHORT,
                size=abs(units) / self.UNITS_PER_LOT,
                entry_price=float(leg.get("averagePrice") or 0.0),
                unrealized_pnl=float(leg.get("unrealizedPL") or 0.0),
            )
        return out

    def submit(self, order: Order) -> Order:
        self._require_connected()
        units = int(order.size * self.UNITS_PER_LOT) * order.side.sign
        body = {
            "order": {
                "instrument": self._to_instrument(order.symbol),
                "units": str(units),
                "type": order.order_type.value.upper(),
                "timeInForce": "FOK" if order.order_type is OrderType.MARKET else "GTC",
                "positionFill": "DEFAULT",
            }
        }
        if order.limit_price is not None:
            body["order"]["price"] = f"{order.limit_price:.5f}"
        resp = self._session.post(
            f"{self.base_url}/accounts/{self.account_id}/orders", json=body, timeout=15
        )
        if resp.status_code >= 400:
            raise BrokerError(f"{self.name}: order rejected: {resp.text[:300]}")
        data = resp.json()
        tx = data.get("orderFillTransaction") or {}
        order.broker_id = str(tx.get("id") or data.get("orderCreateTransaction", {}).get("id", ""))
        order.status = OrderStatus.FILLED if tx else OrderStatus.PENDING
        order.filled_price = float(tx["price"]) if tx.get("price") else None
        order.filled_size = abs(float(tx.get("units", 0))) / self.UNITS_PER_LOT
        return order

    def cancel(self, order: Order) -> None:
        self._require_connected()
        if order.broker_id:
            self._session.put(
                f"{self.base_url}/accounts/{self.account_id}/orders/{order.broker_id}/cancel",
                timeout=15,
            )
        order.status = OrderStatus.CANCELLED

    def latest_bars(self, symbol: str, timeframe_minutes: int, count: int) -> List[Bar]:
        self._require_connected()
        granularity = f"M{timeframe_minutes}" if timeframe_minutes < 60 else f"H{timeframe_minutes // 60}"
        data = self._get(
            f"/instruments/{self._to_instrument(symbol)}/candles",
            granularity=granularity, count=min(count, 5000), price="M",
        )
        bars = []
        for c in data.get("candles", []):
            if not c.get("complete"):
                continue  # never act on a forming candle
            mid = c["mid"]
            bars.append(
                Bar(
                    ts=datetime.fromisoformat(c["time"].replace("Z", "+00:00")),
                    open=float(mid["o"]), high=float(mid["h"]),
                    low=float(mid["l"]), close=float(mid["c"]),
                    volume=float(c.get("volume", 0)), symbol=symbol,
                )
            )
        return bars

    @staticmethod
    def _to_instrument(symbol: str) -> str:
        return f"{symbol[:3]}_{symbol[3:]}" if len(symbol) == 6 else symbol


class TradovateFuturesBroker(_LiveAdapter):
    """Futures via the Tradovate REST/WebSocket API.

    Futures-specific notes:
      * Sizes are whole contracts; the engine's size_step of 1 enforces this.
      * Access tokens expire (~90 min) and must be renewed — `_ensure_token()`
        is where that goes before any real use.
      * Most prop futures firms route through Rithmic or CQG rather than
        Tradovate. The interface is the same shape; swap the transport.
    """

    name = "tradovate"
    supports_live = True
    required_env = ("TRADOVATE_USERNAME", "TRADOVATE_PASSWORD", "TRADOVATE_APP_ID", "TRADOVATE_CID", "TRADOVATE_SEC")
    sdk_hint = "pip install requests"

    DEMO_URL = "https://demo.tradovateapi.com/v1"
    LIVE_URL = "https://live.tradovateapi.com/v1"

    def _connect_impl(self) -> None:
        try:
            import requests  # noqa: PLC0415
        except ImportError as exc:
            raise BrokerError(f"{self.name}: {self.sdk_hint}") from exc
        creds = self._credentials()
        self.base_url = self.base_url or (self.DEMO_URL if self.dry_run else self.LIVE_URL)
        self._session = requests.Session()
        resp = self._session.post(
            f"{self.base_url}/auth/accesstokenrequest",
            json={
                "name": creds["TRADOVATE_USERNAME"],
                "password": creds["TRADOVATE_PASSWORD"],
                "appId": creds["TRADOVATE_APP_ID"],
                "appVersion": "1.0",
                "cid": creds["TRADOVATE_CID"],
                "sec": creds["TRADOVATE_SEC"],
            },
            timeout=20,
        )
        if resp.status_code >= 400:
            raise BrokerError(f"{self.name}: auth failed: {resp.text[:200]}")
        data = resp.json()
        token = data.get("accessToken")
        if not token:
            raise BrokerError(f"{self.name}: auth returned no token: {data}")
        self._token_expires = data.get("expirationTime")
        self._account_id = data.get("userId")
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    def _ensure_token(self) -> None:
        """Renew before expiry. Tradovate tokens are short-lived; a session
        that runs an evaluation day WILL cross an expiry boundary."""
        if not self._token_expires:
            return
        expires = datetime.fromisoformat(str(self._token_expires).replace("Z", "+00:00"))
        if expires - datetime.now(timezone.utc) < timedelta(minutes=5):
            self._connect_impl()

    def account_balance(self) -> float:
        self._require_connected()
        self._ensure_token()
        resp = self._session.get(f"{self.base_url}/cashBalance/list", timeout=15)
        rows = resp.json() if resp.status_code < 400 else []
        return float(rows[0]["amount"]) if rows else 0.0

    def positions(self) -> Dict[str, BrokerPosition]:
        self._require_connected()
        self._ensure_token()
        resp = self._session.get(f"{self.base_url}/position/list", timeout=15)
        out: Dict[str, BrokerPosition] = {}
        for p in resp.json() if resp.status_code < 400 else []:
            net = float(p.get("netPos") or 0)
            if net == 0:
                continue
            symbol = str(p.get("contractId"))
            out[symbol] = BrokerPosition(
                symbol=symbol,
                side=Side.LONG if net > 0 else Side.SHORT,
                size=abs(net),
                entry_price=float(p.get("netPrice") or 0.0),
            )
        return out

    def submit(self, order: Order) -> Order:
        self._require_connected()
        self._ensure_token()
        body = {
            "accountId": self._account_id,
            "symbol": order.symbol,
            "orderQty": int(order.size),
            "action": "Buy" if order.side is Side.LONG else "Sell",
            "orderType": {"market": "Market", "limit": "Limit", "stop": "Stop"}[order.order_type.value],
            "isAutomated": True,
        }
        if order.limit_price is not None:
            body["price"] = order.limit_price
        if order.stop_price is not None:
            body["stopPrice"] = order.stop_price
        resp = self._session.post(f"{self.base_url}/order/placeorder", json=body, timeout=15)
        if resp.status_code >= 400:
            raise BrokerError(f"{self.name}: order rejected: {resp.text[:300]}")
        data = resp.json()
        if data.get("failureReason"):
            order.status = OrderStatus.REJECTED
            order.message = str(data["failureReason"])
            return order
        order.broker_id = str(data.get("orderId"))
        order.status = OrderStatus.PENDING
        return order

    def cancel(self, order: Order) -> None:
        self._require_connected()
        if order.broker_id:
            self._session.post(
                f"{self.base_url}/order/cancelorder", json={"orderId": int(order.broker_id)}, timeout=15
            )
        order.status = OrderStatus.CANCELLED

    def latest_bars(self, symbol: str, timeframe_minutes: int, count: int) -> List[Bar]:
        raise NotImplementedError(
            "Tradovate streams bars over WebSocket (md/getChart), not REST. "
            "Wire a market-data feed and push bars into the engine, or use a "
            "separate data vendor for bars and Tradovate for execution only."
        )
