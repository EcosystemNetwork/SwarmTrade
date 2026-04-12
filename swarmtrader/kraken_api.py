"""Native Kraken REST API client with HMAC-SHA512 authentication.

Replaces CLI subprocess dependency with direct aiohttp calls.
Supports all Kraken REST endpoints needed for trading:
- Public: Ticker, OrderBook, OHLC, AssetPairs
- Private: Balance, AddOrder, CancelOrder, OpenOrders, TradesHistory, GetWebSocketsToken

Authentication follows Kraken's spec:
  API-Sign = base64(HMAC-SHA512(url_path + SHA256(nonce + POST_data), base64_decode(api_secret)))
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from dataclasses import dataclass, field

import aiohttp

log = logging.getLogger("swarm.kraken_api")

_BASE_URL = "https://api.kraken.com"


# ---------------------------------------------------------------------------
# Rate Limiter — models Kraken's exact counter/decay system
# ---------------------------------------------------------------------------
@dataclass
class _TierSpec:
    max_counter: int
    decay_per_sec: float


_TIERS: dict[str, _TierSpec] = {
    "starter":      _TierSpec(max_counter=15, decay_per_sec=0.33),
    "intermediate": _TierSpec(max_counter=20, decay_per_sec=0.5),
    "pro":          _TierSpec(max_counter=20, decay_per_sec=1.0),
}

# Endpoints that cost +2 instead of +1
_HEAVY_ENDPOINTS = frozenset({
    "/0/private/Ledgers", "/0/private/TradesHistory",
    "/0/private/QueryLedgers", "/0/private/QueryTrades",
})


class KrakenRateLimiter:
    """Kraken-specific rate limiter with counter decay per API tier.

    The counter increases with each request and decays continuously.
    If counter would exceed max, we sleep until it decays enough.
    Trading endpoints (AddOrder, CancelOrder) have a SEPARATE per-pair limiter
    managed by Kraken — we don't model that here, but we do track the general counter.
    """

    def __init__(self, tier: str = "starter"):
        spec = _TIERS.get(tier.lower(), _TIERS["starter"])
        self.max_counter = spec.max_counter
        self.decay_per_sec = spec.decay_per_sec
        self._counter: float = 0.0
        self._last_decay_ts: float = time.monotonic()
        self._lock = asyncio.Lock()

    def _decay(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_decay_ts
        self._counter = max(0.0, self._counter - elapsed * self.decay_per_sec)
        self._last_decay_ts = now

    async def acquire(self, endpoint: str = "") -> float:
        """Wait until we can make a request. Returns seconds waited."""
        cost = 2.0 if endpoint in _HEAVY_ENDPOINTS else 1.0
        async with self._lock:
            self._decay()
            if self._counter + cost > self.max_counter:
                wait = (self._counter + cost - self.max_counter) / self.decay_per_sec
                log.debug("Rate limiter: waiting %.2fs (counter=%.1f/%d)",
                          wait, self._counter, self.max_counter)
                await asyncio.sleep(wait)
                self._decay()
            self._counter += cost
            return 0.0

    @property
    def headroom(self) -> float:
        """How many requests can be made right now without waiting."""
        self._decay()
        return max(0.0, self.max_counter - self._counter)


# ---------------------------------------------------------------------------
# Kraken REST Client
# ---------------------------------------------------------------------------
@dataclass
class KrakenAPIConfig:
    """Kraken API connection configuration."""
    api_key: str = ""
    api_secret: str = ""
    tier: str = "starter"          # starter | intermediate | pro
    base_url: str = _BASE_URL


class KrakenRESTClient:
    """Direct Kraken REST API client using aiohttp.

    Handles HMAC-SHA512 authentication, rate limiting, and JSON parsing.
    All methods return the 'result' field from Kraken's response.
    Raises RuntimeError on API errors.
    """

    def __init__(self, config: KrakenAPIConfig | None = None):
        import os
        self._cfg = config or KrakenAPIConfig(
            api_key=os.getenv("KRAKEN_API_KEY", ""),
            api_secret=os.getenv("KRAKEN_PRIVATE_KEY", ""),
            tier=os.getenv("KRAKEN_TIER", "starter"),
        )
        self._session: aiohttp.ClientSession | None = None
        self._limiter = KrakenRateLimiter(self._cfg.tier)
        self._nonce_offset: int = 0  # monotonic nonce guard

    # -- lifecycle ----------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"User-Agent": "SwarmTrader/1.0"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # -- auth ---------------------------------------------------------------

    def _sign(self, url_path: str, data: dict) -> dict[str, str]:
        """Generate Kraken API-Sign header using HMAC-SHA512."""
        if not self._cfg.api_key or not self._cfg.api_secret:
            raise RuntimeError("Kraken API keys not configured")

        nonce = str(int(time.time() * 1000))
        data["nonce"] = nonce

        # POST body as url-encoded string
        post_data = urllib.parse.urlencode(data)

        # SHA256(nonce + POST_data)
        sha256_digest = hashlib.sha256(
            (nonce + post_data).encode("utf-8")
        ).digest()

        # HMAC-SHA512(url_path + sha256_digest, base64_decode(secret))
        secret_bytes = base64.b64decode(self._cfg.api_secret)
        hmac_digest = hmac.new(
            secret_bytes,
            url_path.encode("utf-8") + sha256_digest,
            hashlib.sha512,
        ).digest()

        return {
            "API-Key": self._cfg.api_key,
            "API-Sign": base64.b64encode(hmac_digest).decode("utf-8"),
            "Content-Type": "application/x-www-form-urlencoded",
        }

    # -- low-level request --------------------------------------------------

    async def _public(self, endpoint: str, params: dict | None = None) -> dict:
        """Make a public (unauthenticated) GET/POST request."""
        url_path = f"/0/public/{endpoint}"
        url = f"{self._cfg.base_url}{url_path}"
        await self._limiter.acquire(url_path)

        session = await self._ensure_session()
        async with session.post(url, data=params or {}) as resp:
            body = await resp.json()

        errors = body.get("error", [])
        if errors:
            raise RuntimeError(f"Kraken API error: {', '.join(errors)}")
        return body.get("result", {})

    async def _private(self, endpoint: str, data: dict | None = None) -> dict:
        """Make a private (authenticated) POST request."""
        url_path = f"/0/private/{endpoint}"
        url = f"{self._cfg.base_url}{url_path}"
        await self._limiter.acquire(url_path)

        payload = dict(data or {})
        headers = self._sign(url_path, payload)
        post_data = urllib.parse.urlencode(payload)

        session = await self._ensure_session()
        async with session.post(url, data=post_data, headers=headers) as resp:
            body = await resp.json()

        errors = body.get("error", [])
        if errors:
            raise RuntimeError(f"Kraken API error: {', '.join(errors)}")
        return body.get("result", {})

    # -- public endpoints ---------------------------------------------------

    async def get_ticker(self, pairs: list[str]) -> dict:
        """Get ticker information for one or more pairs.

        Returns dict keyed by Kraken pair name, each containing:
          a: [ask_price, whole_lot_volume, lot_volume]
          b: [bid_price, whole_lot_volume, lot_volume]
          c: [last_price, lot_volume]
          v: [today_volume, 24h_volume]
          p: [today_vwap, 24h_vwap]
          etc.
        """
        pair_str = ",".join(pairs)
        return await self._public("Ticker", {"pair": pair_str})

    async def get_order_book(self, pair: str, count: int = 25) -> dict:
        """Get L2 order book for a pair.

        Returns dict with pair key containing:
          asks: [[price, volume, timestamp], ...]
          bids: [[price, volume, timestamp], ...]
        """
        return await self._public("Depth", {"pair": pair, "count": count})

    async def get_ohlc(self, pair: str, interval: int = 1,
                       since: int | None = None) -> dict:
        """Get OHLC candle data.

        interval: minutes (1, 5, 15, 30, 60, 240, 1440, 10080, 21600)
        """
        params: dict = {"pair": pair, "interval": interval}
        if since is not None:
            params["since"] = since
        return await self._public("OHLC", params)

    async def get_asset_pairs(self, pairs: list[str] | None = None) -> dict:
        """Get tradeable asset pair info (fees, precision, min order, etc.)."""
        params: dict = {}
        if pairs:
            params["pair"] = ",".join(pairs)
        return await self._public("AssetPairs", params)

    async def get_system_status(self) -> dict:
        """Get Kraken system status (online, maintenance, etc.)."""
        return await self._public("SystemStatus")

    # -- private endpoints: account -----------------------------------------

    async def get_balance(self) -> dict:
        """Get all cash balances, net of pending withdrawals."""
        return await self._private("Balance")

    async def get_trade_balance(self, asset: str = "ZUSD") -> dict:
        """Get trade balance summary (equity, margin, unrealized PnL)."""
        return await self._private("TradeBalance", {"asset": asset})

    async def get_open_orders(self, trades: bool = False) -> dict:
        """Get all currently open orders."""
        data: dict = {}
        if trades:
            data["trades"] = True
        return await self._private("OpenOrders", data)

    async def get_closed_orders(self, trades: bool = False,
                                start: int | None = None,
                                end: int | None = None) -> dict:
        """Get closed orders (paginated, 50 at a time)."""
        data: dict = {}
        if trades:
            data["trades"] = True
        if start is not None:
            data["start"] = start
        if end is not None:
            data["end"] = end
        return await self._private("ClosedOrders", data)

    async def get_trades_history(self, trades: bool = False) -> dict:
        """Get trade history (50 results at a time, most recent first).

        Note: costs +2 to rate limit counter.
        """
        data: dict = {}
        if trades:
            data["trades"] = True
        return await self._private("TradesHistory", data)

    async def get_open_positions(self) -> dict:
        """Get open margin positions."""
        return await self._private("OpenPositions")

    # -- private endpoints: trading -----------------------------------------

    async def add_order(
        self,
        pair: str,
        side: str,           # "buy" or "sell"
        order_type: str,     # "market", "limit", "stop-loss", etc.
        volume: float,
        *,
        price: float | None = None,
        price2: float | None = None,
        trigger: str | None = None,
        leverage: str | None = None,
        oflags: str | None = None,
        timeinforce: str | None = None,
        starttm: str | None = None,
        expiretm: str | None = None,
        cl_ord_id: str | None = None,
        close_ordertype: str | None = None,
        close_price: float | None = None,
        close_price2: float | None = None,
        validate: bool = False,
        display_volume: float | None = None,
    ) -> dict:
        """Place an order on Kraken.

        Supports all order types: market, limit, stop-loss, stop-loss-limit,
        take-profit, take-profit-limit, trailing-stop, trailing-stop-limit,
        iceberg, settle-position.

        Args:
            pair: Trading pair (e.g., "ETHUSD" or "ETH/USD")
            side: "buy" or "sell"
            order_type: One of the supported order types
            volume: Order quantity in base asset
            price: Price for limit/stop/take-profit orders
            price2: Secondary price for stop-limit/take-profit-limit
            trigger: Trigger condition ("last", "index")
            leverage: e.g., "2" for 2x
            oflags: Order flags ("post", "fcib", "fciq", "nompp", "viqc")
            timeinforce: "gtc", "gtd", "ioc"
            starttm: Scheduled start time
            expiretm: Expiration time (required for "gtd")
            cl_ord_id: Client order ID for tracking
            close_ordertype: Conditional close order type (bracket order)
            close_price: Conditional close price
            close_price2: Conditional close secondary price
            validate: If True, validate only (no execution)
            display_volume: Visible volume for iceberg orders

        Returns dict with:
            descr: Order description
            txid: List of transaction IDs
        """
        data: dict = {
            "pair": pair,
            "type": side,
            "ordertype": order_type,
            "volume": f"{volume:.8f}",
        }
        if price is not None:
            data["price"] = f"{price:.8f}"
        if price2 is not None:
            data["price2"] = f"{price2:.8f}"
        if trigger:
            data["trigger"] = trigger
        if leverage:
            data["leverage"] = leverage
        if oflags:
            data["oflags"] = oflags
        if timeinforce:
            data["timeinforce"] = timeinforce
        if starttm:
            data["starttm"] = starttm
        if expiretm:
            data["expiretm"] = expiretm
        if cl_ord_id:
            data["cl_ord_id"] = cl_ord_id
        if close_ordertype:
            data["close[ordertype]"] = close_ordertype
        if close_price is not None:
            data["close[price]"] = f"{close_price:.8f}"
        if close_price2 is not None:
            data["close[price2]"] = f"{close_price2:.8f}"
        if validate:
            data["validate"] = "true"
        if display_volume is not None:
            data["displayvol"] = f"{display_volume:.8f}"

        return await self._private("AddOrder", data)

    async def query_orders(self, txids: list[str], trades: bool = False) -> dict:
        """Query order status by txid(s). Returns details even for closed orders."""
        data: dict = {"txid": ",".join(txids)}
        if trades:
            data["trades"] = True
        return await self._private("QueryOrders", data)

    async def cancel_order(self, txid: str) -> dict:
        """Cancel a single open order by transaction ID or cl_ord_id."""
        return await self._private("CancelOrder", {"txid": txid})

    async def cancel_all(self) -> dict:
        """Cancel ALL open orders. Returns count of cancelled orders."""
        return await self._private("CancelAll")

    async def cancel_all_after(self, timeout_seconds: int) -> dict:
        """Dead man's switch: cancel all orders after timeout.

        Set timeout=0 to disable. Kraken will cancel all orders if no
        subsequent call is made within the timeout period.
        """
        return await self._private("CancelAllOrdersAfter",
                                   {"timeout": timeout_seconds})

    # -- private endpoints: websocket token ---------------------------------

    async def get_ws_token(self) -> str:
        """Get a WebSocket authentication token (valid 15 minutes).

        The token does NOT expire once a WebSocket connection is established.
        """
        result = await self._private("GetWebSocketsToken")
        return result.get("token", "")

    # -- validation ---------------------------------------------------------

    async def validate_keys(self) -> tuple[bool, str]:
        """Pre-flight check: verify API keys are valid by calling Balance."""
        try:
            result = await self.get_balance()
            return True, f"API keys valid, {len(result)} assets in balance"
        except RuntimeError as e:
            return False, f"API key validation failed: {e}"

    @property
    def rate_limit_headroom(self) -> float:
        """Current rate limit headroom (requests available without waiting)."""
        return self._limiter.headroom


# ---------------------------------------------------------------------------
# Module-level singleton for shared use across the application
# ---------------------------------------------------------------------------
_shared_client: KrakenRESTClient | None = None


def get_client(config: KrakenAPIConfig | None = None) -> KrakenRESTClient:
    """Get or create the shared KrakenRESTClient instance."""
    global _shared_client
    if _shared_client is None:
        _shared_client = KrakenRESTClient(config)
    return _shared_client


async def close_client():
    """Close the shared client session (call on shutdown)."""
    global _shared_client
    if _shared_client:
        await _shared_client.close()
        _shared_client = None
