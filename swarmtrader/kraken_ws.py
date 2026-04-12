"""Kraken WebSocket v2 client for real-time market data and private channels.

Public channels (wss://ws.kraken.com/v2):
  - ticker: Top-of-book bid/offer + recent trade data
  - book: L2 order book with configurable depth (10, 25, 100, 500, 1000)
  - trade: Real-time trade feed
  - ohlc: Candlestick data

Private channels (wss://ws-auth.kraken.com/v2):
  - executions: Consolidated open orders + fills (replaces v1 openOrders + ownTrades)

Order management via WebSocket:
  - add_order: Place orders with lower latency than REST
  - cancel_order: Cancel individual orders
  - cancel_all: Emergency cancel all open orders
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

import aiohttp

from .core import Bus, MarketSnapshot
from .kraken_api import KrakenRESTClient, get_client

log = logging.getLogger("swarm.kraken_ws")

_PUBLIC_WS_URL = "wss://ws.kraken.com/v2"
_PRIVATE_WS_URL = "wss://ws-auth.kraken.com/v2"

# Reconnection constants
_RECONNECT_BASE = 2.0
_RECONNECT_MAX = 60.0


def _compute_backoff(attempt: int) -> float:
    import random
    delay = min(_RECONNECT_BASE * (2.0 ** attempt), _RECONNECT_MAX)
    return delay + delay * random.uniform(0.1, 0.25)


# ---------------------------------------------------------------------------
# Order Book Snapshot — maintained from WS incremental updates
# ---------------------------------------------------------------------------
@dataclass
class OrderBookSnapshot:
    """L2 order book maintained from WebSocket updates."""
    pair: str
    bids: list[list[float]] = field(default_factory=list)  # [[price, qty], ...]
    asks: list[list[float]] = field(default_factory=list)
    ts: float = 0.0

    @property
    def best_bid(self) -> float:
        return self.bids[0][0] if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0][0] if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        if self.best_bid and self.best_ask:
            return (self.best_bid + self.best_ask) / 2.0
        return self.best_bid or self.best_ask

    @property
    def spread(self) -> float:
        if self.best_bid and self.best_ask:
            return self.best_ask - self.best_bid
        return 0.0

    @property
    def spread_bps(self) -> float:
        mid = self.mid_price
        return (self.spread / mid * 10_000) if mid > 0 else 0.0

    @property
    def depth_levels(self) -> int:
        return max(len(self.bids), len(self.asks))


# ---------------------------------------------------------------------------
# WebSocket v2 Client — public + private channels
# ---------------------------------------------------------------------------
class KrakenWSv2Client:
    """Direct Kraken WebSocket v2 client.

    Handles public and private channel subscriptions, heartbeat,
    auto-reconnect, and message routing.
    """

    def __init__(self, bus: Bus, client: KrakenRESTClient | None = None):
        self.bus = bus
        self._rest_client = client or get_client()
        self._stop = False

        # Connection state
        self._pub_ws: aiohttp.ClientWebSocketResponse | None = None
        self._priv_ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws_token: str = ""

        # Subscriptions
        self._pub_subscriptions: list[dict] = []
        self._priv_subscriptions: list[dict] = []

        # Order book state — maintained from incremental updates
        self._books: dict[str, OrderBookSnapshot] = {}

        # Request tracking for order management
        self._req_counter = 0
        self._pending_requests: dict[int, asyncio.Future] = {}

        # Stats
        self._last_msg_ts: float = 0.0
        self._msg_count: int = 0

    def stop(self):
        self._stop = True

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._pub_ws and not self._pub_ws.closed:
            await self._pub_ws.close()
        if self._priv_ws and not self._priv_ws.closed:
            await self._priv_ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # -- subscription helpers -----------------------------------------------

    def subscribe_ticker(self, pairs: list[str]):
        """Subscribe to ticker channel for given pairs."""
        self._pub_subscriptions.append({
            "method": "subscribe",
            "params": {
                "channel": "ticker",
                "symbol": pairs,
                "event_trigger": "trades",
            },
        })

    def subscribe_book(self, pairs: list[str], depth: int = 25):
        """Subscribe to L2 order book channel."""
        self._pub_subscriptions.append({
            "method": "subscribe",
            "params": {
                "channel": "book",
                "symbol": pairs,
                "depth": depth,
            },
        })

    def subscribe_trades(self, pairs: list[str]):
        """Subscribe to real-time trade feed."""
        self._pub_subscriptions.append({
            "method": "subscribe",
            "params": {
                "channel": "trade",
                "symbol": pairs,
            },
        })

    def subscribe_ohlc(self, pairs: list[str], interval: int = 1):
        """Subscribe to OHLC candle updates."""
        self._pub_subscriptions.append({
            "method": "subscribe",
            "params": {
                "channel": "ohlc",
                "symbol": pairs,
                "interval": interval,
            },
        })

    def subscribe_executions(self):
        """Subscribe to private executions channel (fills + open orders)."""
        self._priv_subscriptions.append({
            "method": "subscribe",
            "params": {
                "channel": "executions",
                "snap_orders": True,
                "snap_trades": True,
            },
        })

    # -- public WebSocket ---------------------------------------------------

    async def run_public(self):
        """Connect to public WS and process messages with auto-reconnect."""
        attempt = 0
        while not self._stop:
            try:
                await self._connect_public()
                attempt = 0  # reset on successful connection
                if self._stop:
                    break
            except Exception as e:
                attempt += 1
                backoff = _compute_backoff(attempt)
                log.warning("Public WS disconnected (attempt %d, backoff %.1fs): %s",
                            attempt, backoff, e)
                await asyncio.sleep(backoff)

    async def _connect_public(self):
        session = await self._ensure_session()
        log.info("Connecting to Kraken WS v2 (public): %s", _PUBLIC_WS_URL)

        async with session.ws_connect(_PUBLIC_WS_URL) as ws:
            self._pub_ws = ws

            # Send subscriptions
            for sub in self._pub_subscriptions:
                await ws.send_json(sub)
                log.debug("Sent subscription: %s", sub["params"].get("channel"))

            # Message loop
            async for msg in ws:
                if self._stop:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._last_msg_ts = time.time()
                    self._msg_count += 1
                    try:
                        data = json.loads(msg.data)
                        await self._handle_public_msg(data)
                    except (json.JSONDecodeError, Exception) as e:
                        log.debug("WS parse error: %s", e)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    log.warning("Public WS closed: %s", msg.type)
                    break

            self._pub_ws = None

    async def _handle_public_msg(self, data: dict):
        """Route incoming public WS messages to handlers."""
        channel = data.get("channel", "")
        msg_type = data.get("type", "")

        if channel == "heartbeat":
            return

        if channel == "status":
            system = data.get("data", [{}])[0].get("system", "unknown")
            log.info("Kraken WS system status: %s", system)
            return

        if channel == "ticker":
            await self._on_ticker(data)
        elif channel == "book":
            await self._on_book(data, msg_type)
        elif channel == "trade":
            await self._on_trade(data)
        elif channel == "ohlc":
            await self._on_ohlc(data)

    async def _on_ticker(self, data: dict):
        """Process ticker updates → publish market.snapshot."""
        ticks = data.get("data", [])
        prices = {}
        for tick in ticks:
            symbol = tick.get("symbol", "").split("/")[0]
            if not symbol:
                continue
            ask = float(tick.get("ask", 0))
            bid = float(tick.get("bid", 0))
            if ask > 0 and bid > 0:
                # Normalize XBT → BTC
                if symbol == "XBT":
                    symbol = "BTC"
                prices[symbol] = (ask + bid) / 2.0

        if prices:
            snap = MarketSnapshot(ts=time.time(), prices=prices, gas_gwei=0.0)
            await self.bus.publish("market.snapshot", snap)

    async def _on_book(self, data: dict, msg_type: str):
        """Process order book snapshots and updates → maintain local book."""
        entries = data.get("data", [])
        for entry in entries:
            symbol = entry.get("symbol", "")
            if not symbol:
                continue

            norm_symbol = symbol.split("/")[0]
            if norm_symbol == "XBT":
                norm_symbol = "BTC"

            if msg_type == "snapshot":
                # Full snapshot — replace book
                bids = [[float(b["price"]), float(b["qty"])]
                        for b in entry.get("bids", [])]
                asks = [[float(a["price"]), float(a["qty"])]
                        for a in entry.get("asks", [])]
                self._books[norm_symbol] = OrderBookSnapshot(
                    pair=symbol, bids=bids, asks=asks, ts=time.time(),
                )
            elif msg_type == "update":
                book = self._books.get(norm_symbol)
                if not book:
                    continue
                # Apply incremental updates
                for bid in entry.get("bids", []):
                    price, qty = float(bid["price"]), float(bid["qty"])
                    self._update_side(book.bids, price, qty, reverse=True)
                for ask in entry.get("asks", []):
                    price, qty = float(ask["price"]), float(ask["qty"])
                    self._update_side(book.asks, price, qty, reverse=False)
                book.ts = time.time()

            # Publish book update
            book = self._books.get(norm_symbol)
            if book:
                await self.bus.publish("market.book", {
                    "symbol": norm_symbol,
                    "pair": symbol,
                    "best_bid": book.best_bid,
                    "best_ask": book.best_ask,
                    "mid": book.mid_price,
                    "spread_bps": book.spread_bps,
                    "depth_levels": book.depth_levels,
                    "bids_top5": book.bids[:5],
                    "asks_top5": book.asks[:5],
                    "ts": book.ts,
                })

    @staticmethod
    def _update_side(levels: list[list[float]], price: float, qty: float,
                     reverse: bool):
        """Update a single side of the order book.

        If qty == 0, remove the level. Otherwise insert/update at correct position.
        """
        # Remove existing level at this price
        levels[:] = [l for l in levels if abs(l[0] - price) > 1e-10]

        if qty > 0:
            # Insert at correct sorted position
            inserted = False
            for i, l in enumerate(levels):
                if (reverse and price > l[0]) or (not reverse and price < l[0]):
                    levels.insert(i, [price, qty])
                    inserted = True
                    break
            if not inserted:
                levels.append([price, qty])

    async def _on_trade(self, data: dict):
        """Process real-time trades → publish market.trades."""
        trades = data.get("data", [])
        for trade in trades:
            symbol = trade.get("symbol", "").split("/")[0]
            if symbol == "XBT":
                symbol = "BTC"
            await self.bus.publish("market.trades", {
                "symbol": symbol,
                "price": float(trade.get("price", 0)),
                "qty": float(trade.get("qty", 0)),
                "side": trade.get("side", ""),
                "ts": trade.get("timestamp", ""),
            })

    async def _on_ohlc(self, data: dict):
        """Process OHLC candle updates → publish market.ohlc."""
        candles = data.get("data", [])
        for candle in candles:
            symbol = candle.get("symbol", "").split("/")[0]
            if symbol == "XBT":
                symbol = "BTC"
            await self.bus.publish("market.ohlc", {
                "symbol": symbol,
                "open": float(candle.get("open", 0)),
                "high": float(candle.get("high", 0)),
                "low": float(candle.get("low", 0)),
                "close": float(candle.get("close", 0)),
                "volume": float(candle.get("volume", 0)),
                "vwap": float(candle.get("vwap", 0)),
                "interval_begin": candle.get("interval_begin", ""),
                "ts": candle.get("timestamp", ""),
            })

    # -- private WebSocket --------------------------------------------------

    async def run_private(self):
        """Connect to private WS and process executions with auto-reconnect."""
        attempt = 0
        while not self._stop:
            try:
                # Get fresh WS token
                self._ws_token = await self._rest_client.get_ws_token()
                if not self._ws_token:
                    log.error("Failed to get WS token, retrying...")
                    await asyncio.sleep(5.0)
                    continue

                await self._connect_private()
                attempt = 0
                if self._stop:
                    break
            except Exception as e:
                attempt += 1
                backoff = _compute_backoff(attempt)
                log.warning("Private WS disconnected (attempt %d, backoff %.1fs): %s",
                            attempt, backoff, e)
                await asyncio.sleep(backoff)

    async def _connect_private(self):
        session = await self._ensure_session()
        log.info("Connecting to Kraken WS v2 (private): %s", _PRIVATE_WS_URL)

        async with session.ws_connect(_PRIVATE_WS_URL) as ws:
            self._priv_ws = ws

            # Send subscriptions with token
            for sub in self._priv_subscriptions:
                msg = dict(sub)
                msg["params"]["token"] = self._ws_token
                await ws.send_json(msg)
                log.debug("Sent private subscription: %s", msg["params"].get("channel"))

            # Message loop
            async for msg in ws:
                if self._stop:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self._last_msg_ts = time.time()
                    try:
                        data = json.loads(msg.data)
                        await self._handle_private_msg(data)
                    except (json.JSONDecodeError, Exception) as e:
                        log.debug("Private WS parse error: %s", e)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    log.warning("Private WS closed: %s", msg.type)
                    break

            self._priv_ws = None

    async def _handle_private_msg(self, data: dict):
        """Route incoming private WS messages."""
        channel = data.get("channel", "")
        method = data.get("method", "")

        if channel == "heartbeat":
            return

        # Handle responses to our requests (add_order, cancel_order, etc.)
        if method:
            req_id = data.get("req_id")
            if req_id is not None and req_id in self._pending_requests:
                future = self._pending_requests.pop(req_id)
                if not future.done():
                    future.set_result(data)
            return

        if channel == "executions":
            await self._on_execution(data)

    async def _on_execution(self, data: dict):
        """Process execution events → publish order lifecycle events."""
        msg_type = data.get("type", "")
        executions = data.get("data", [])

        for exec_data in executions:
            exec_type = exec_data.get("exec_type", "")
            order_id = exec_data.get("order_id", "")
            cl_ord_id = exec_data.get("cl_ord_id", "")
            symbol = exec_data.get("symbol", "").split("/")[0]
            if symbol == "XBT":
                symbol = "BTC"

            event = {
                "exec_type": exec_type,
                "order_id": order_id,
                "cl_ord_id": cl_ord_id,
                "symbol": symbol,
                "side": exec_data.get("side", ""),
                "order_type": exec_data.get("order_type", ""),
                "order_status": exec_data.get("order_status", ""),
                "order_qty": float(exec_data.get("order_qty", 0)),
                "cum_qty": float(exec_data.get("cum_qty", 0)),
                "avg_price": float(exec_data.get("avg_price", 0)),
                "fee_usd": float(exec_data.get("fee_usd_equiv", 0)),
                "ts": exec_data.get("timestamp", ""),
                "snapshot": msg_type == "snapshot",
            }

            # Fill-specific fields
            if exec_type == "trade":
                event["last_qty"] = float(exec_data.get("last_qty", 0))
                event["last_price"] = float(exec_data.get("last_price", 0))
                event["liquidity"] = exec_data.get("liquidity_ind", "")  # "m" or "t"

            await self.bus.publish("order.execution", event)

            # Also publish typed events for simpler consumers
            if exec_type == "filled":
                await self.bus.publish("order.filled", {
                    "intent_id": cl_ord_id,
                    "txid": order_id,
                    "filled_qty": event["cum_qty"],
                    "avg_price": event["avg_price"],
                    "fee_usd": event["fee_usd"],
                    "side": event["side"],
                    "asset": symbol,
                })
            elif exec_type == "trade":
                cum = event["cum_qty"]
                total = event["order_qty"]
                if cum < total - 1e-9:
                    await self.bus.publish("order.partial_fill", {
                        "intent_id": cl_ord_id,
                        "txid": order_id,
                        "filled_qty": cum,
                        "remaining_qty": total - cum,
                        "total_qty": total,
                        "progress": cum / max(total, 1e-9),
                        "last_price": event["last_price"],
                        "last_qty": event["last_qty"],
                    })
            elif exec_type in ("canceled", "expired"):
                await self.bus.publish("order.cancelled", {
                    "intent_id": cl_ord_id,
                    "txid": order_id,
                    "reason": exec_type,
                })

    # -- WebSocket order management -----------------------------------------

    def _next_req_id(self) -> int:
        self._req_counter += 1
        return self._req_counter

    async def _send_private(self, msg: dict, timeout: float = 10.0) -> dict:
        """Send a message on the private WS and wait for the response."""
        if not self._priv_ws or self._priv_ws.closed:
            raise RuntimeError("Private WebSocket not connected")

        req_id = self._next_req_id()
        msg["req_id"] = req_id

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_requests[req_id] = future

        await self._priv_ws.send_json(msg)

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise RuntimeError(f"WS request {req_id} timed out after {timeout}s")

        if not result.get("success", False):
            error = result.get("error", "unknown error")
            raise RuntimeError(f"WS order error: {error}")

        return result.get("result", {})

    async def ws_add_order(self, *, pair: str, side: str, order_type: str,
                           volume: float, price: float | None = None,
                           cl_ord_id: str | None = None,
                           **kwargs) -> dict:
        """Place an order via WebSocket (lower latency than REST)."""
        params: dict = {
            "order_type": order_type,
            "side": side,
            "order_qty": volume,
            "symbol": pair,
            "token": self._ws_token,
        }
        if price is not None:
            params["limit_price"] = price
        if cl_ord_id:
            params["cl_ord_id"] = cl_ord_id
        for k, v in kwargs.items():
            if v is not None:
                params[k] = v

        return await self._send_private({"method": "add_order", "params": params})

    async def ws_cancel_order(self, order_id: str) -> dict:
        """Cancel a single order via WebSocket."""
        return await self._send_private({
            "method": "cancel_order",
            "params": {"order_id": [order_id], "token": self._ws_token},
        })

    async def ws_cancel_all(self) -> dict:
        """Cancel ALL open orders via WebSocket. Emergency use."""
        return await self._send_private({
            "method": "cancel_all",
            "params": {"token": self._ws_token},
        })

    # -- convenience --------------------------------------------------------

    def get_book(self, symbol: str) -> OrderBookSnapshot | None:
        """Get current order book snapshot for a symbol."""
        return self._books.get(symbol)

    @property
    def books(self) -> dict[str, OrderBookSnapshot]:
        """All maintained order books."""
        return dict(self._books)

    @property
    def connected(self) -> bool:
        return (self._pub_ws is not None and not self._pub_ws.closed)

    @property
    def private_connected(self) -> bool:
        return (self._priv_ws is not None and not self._priv_ws.closed)

    @property
    def stats(self) -> dict:
        return {
            "connected": self.connected,
            "private_connected": self.private_connected,
            "msg_count": self._msg_count,
            "last_msg_ts": self._last_msg_ts,
            "books": list(self._books.keys()),
        }
