"""Core data types, position management, and async pub/sub bus."""
from __future__ import annotations
import asyncio, logging, time, uuid
from dataclasses import dataclass, field
from typing import Literal, Callable, Awaitable, Any

Direction = Literal["long", "short", "flat"]

_MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB


async def safe_json(resp, max_bytes: int = _MAX_RESPONSE_BYTES,
                    read_timeout: float = 10.0) -> dict | list | None:
    """Read and parse JSON from an aiohttp response with size and timeout guard.

    Returns None if response is too large, too slow, or unparseable.
    """
    if resp.content_length and resp.content_length > max_bytes:
        logging.getLogger("swarm.http").warning(
            "Response too large (%d bytes > %d), skipping", resp.content_length, max_bytes)
        return None
    try:
        body = await asyncio.wait_for(resp.read(), timeout=read_timeout)
        if len(body) > max_bytes:
            logging.getLogger("swarm.http").warning(
                "Response body too large (%d bytes), skipping", len(body))
            return None
        import json as _json
        return _json.loads(body)
    except asyncio.TimeoutError:
        logging.getLogger("swarm.http").warning("Response read timed out after %.0fs", read_timeout)
        return None
    except Exception:
        return None

# Kraken fee tiers (taker fees for market orders)
KRAKEN_TAKER_FEE = 0.0026   # 0.26%
KRAKEN_MAKER_FEE = 0.0016   # 0.16%

QUOTE_ASSETS = frozenset({"USD", "USDC", "USDT"})


@dataclass
class MarketSnapshot:
    ts: float
    prices: dict[str, float]
    gas_gwei: float


@dataclass
class Signal:
    agent_id: str
    asset: str
    direction: Direction
    strength: float    # -1..1
    confidence: float  # 0..1
    rationale: str
    ts: float = field(default_factory=time.time)


@dataclass
class OrderSpec:
    """Specifies how an order should be placed on the exchange.

    Supports all Kraken order types:
      market, limit, stop-loss, stop-loss-limit, take-profit,
      take-profit-limit, trailing-stop, trailing-stop-limit,
      iceberg, settle-position

    Bracket orders use close_* fields (Kraken's conditional close).
    """
    order_type: str = "market"          # Kraken order type string
    limit_price: float | None = None    # price for limit orders
    stop_price: float | None = None     # trigger price for stop/take-profit
    stop_limit_price: float | None = None  # limit price for stop-limit orders
    trailing_offset: float | None = None   # offset for trailing stop (absolute or %)
    # Bracket order (conditional close attached to entry)
    close_order_type: str | None = None    # e.g., "stop-loss-limit"
    close_price: float | None = None       # stop-loss trigger price
    close_price2: float | None = None      # take-profit price (or stop-limit limit)
    # Iceberg
    display_volume: float | None = None    # visible volume (native iceberg)
    # Time-in-force
    time_in_force: str | None = None       # "gtc", "gtd", "ioc"
    expire_time: str | None = None         # for "gtd"
    # Order flags
    post_only: bool = False                # maker-only (passive)
    reduce_only: bool = False              # reduce position only


@dataclass
class TradeIntent:
    id: str
    asset_in: str
    asset_out: str
    amount_in: float
    min_out: float
    ttl: float
    supporting: list[Signal] = field(default_factory=list)
    order_spec: OrderSpec | None = None  # None = use default (market)

    @staticmethod
    def new(**kw) -> "TradeIntent":
        return TradeIntent(id=uuid.uuid4().hex[:8], **kw)


@dataclass
class RiskVerdict:
    intent_id: str
    agent_id: str
    approve: bool
    reason: str


@dataclass
class ExecutionReport:
    intent_id: str
    status: Literal["filled", "rejected", "expired", "error", "partial", "submitted", "cancelled"]
    tx_hash: str | None
    fill_price: float | None
    realized_slippage: float | None
    pnl_estimate: float | None
    note: str = ""
    side: str = ""          # "buy" or "sell"
    quantity: float = 0.0   # amount of base asset
    asset: str = ""         # base asset symbol (e.g. "ETH")
    fee_usd: float = 0.0   # fee paid in USD
    # Phase 4: Order lifecycle fields
    partial_fill: bool = False
    filled_quantity: float = 0.0    # cumulative filled so far
    remaining_quantity: float = 0.0  # remaining to fill
    cl_ord_id: str = ""             # client order ID for Kraken tracking


# ---------------------------------------------------------------------------
# Position tracking — real cost-basis and PnL from actual price deltas
# ---------------------------------------------------------------------------
@dataclass
class OpenPosition:
    """Tracks a position in a single asset with real cost accounting."""
    asset: str
    quantity: float = 0.0         # units held (positive only)
    cost_basis: float = 0.0       # total USD spent to acquire
    realized_pnl: float = 0.0    # cumulative closed PnL
    total_fees: float = 0.0      # cumulative fees paid
    trade_count: int = 0

    @property
    def avg_entry(self) -> float:
        return self.cost_basis / self.quantity if self.quantity > 1e-9 else 0.0

    def unrealized_pnl(self, current_price: float) -> float:
        return self.quantity * current_price - self.cost_basis

    def mark_to_market(self, current_price: float) -> float:
        return self.realized_pnl + self.unrealized_pnl(current_price) - self.total_fees


class PortfolioTracker:
    """Central position ledger. Computes real PnL from actual fills."""

    def __init__(self):
        self.positions: dict[str, OpenPosition] = {}
        self.last_prices: dict[str, float] = {}

    def get(self, asset: str) -> OpenPosition:
        if asset not in self.positions:
            self.positions[asset] = OpenPosition(asset=asset)
        return self.positions[asset]

    def buy(self, asset: str, quantity: float, fill_price: float,
            fee_rate: float = KRAKEN_TAKER_FEE) -> tuple[float, float]:
        """Record a buy. Returns (fee_usd, pnl=0 for buys)."""
        pos = self.get(asset)
        cost = quantity * fill_price
        fee = cost * fee_rate
        pos.quantity += quantity
        pos.cost_basis += cost
        pos.total_fees += fee
        pos.trade_count += 1
        return fee, 0.0

    def sell(self, asset: str, quantity: float, fill_price: float,
             fee_rate: float = KRAKEN_TAKER_FEE) -> tuple[float, float]:
        """Record a sell. Returns (fee_usd, realized_pnl).
        PnL = (fill_price - avg_entry) * quantity - fees."""
        pos = self.get(asset)
        quantity = min(quantity, pos.quantity)  # can't sell more than held
        if quantity < 1e-9:
            return 0.0, 0.0
        proceeds = quantity * fill_price
        fee = proceeds * fee_rate
        avg = pos.avg_entry
        cost_of_sold = avg * quantity
        pnl = proceeds - cost_of_sold - fee
        pos.quantity -= quantity
        pos.cost_basis -= cost_of_sold
        pos.realized_pnl += pnl
        pos.total_fees += fee
        pos.trade_count += 1
        if pos.quantity < 1e-9:
            pos.quantity = 0.0
            pos.cost_basis = 0.0
        return fee, pnl

    def update_prices(self, prices: dict[str, float]):
        self.last_prices.update(prices)

    def total_equity(self) -> float:
        return sum(
            pos.mark_to_market(self.last_prices.get(asset, pos.avg_entry))
            for asset, pos in self.positions.items()
            if pos.quantity > 1e-9
        )

    def position_value(self, asset: str) -> float:
        """Market value of a single asset position."""
        pos = self.positions.get(asset)
        if not pos or pos.quantity < 1e-9:
            return 0.0
        return pos.quantity * self.last_prices.get(asset, pos.avg_entry)

    def position_market_value(self) -> float:
        """Sum of (quantity * current_price) for all open positions.

        Use this for wallet equity calculations where cash already accounts
        for cost basis and fees. total_equity() double-counts when added to cash.
        """
        return sum(
            pos.quantity * self.last_prices.get(asset, pos.avg_entry)
            for asset, pos in self.positions.items()
            if pos.quantity > 1e-9
        )

    def total_realized_pnl(self) -> float:
        return sum(pos.realized_pnl for pos in self.positions.values())

    def total_fees(self) -> float:
        return sum(pos.total_fees for pos in self.positions.values())

    def summary(self) -> dict:
        return {
            "positions": {
                a: {
                    "qty": round(p.quantity, 6),
                    "avg_entry": round(p.avg_entry, 2),
                    "cost_basis": round(p.cost_basis, 2),
                    "current_price": round(self.last_prices.get(a, 0), 2),
                    "unrealized_pnl": round(p.unrealized_pnl(self.last_prices.get(a, p.avg_entry)), 4),
                    "realized_pnl": round(p.realized_pnl, 4),
                    "fees": round(p.total_fees, 4),
                    "trades": p.trade_count,
                }
                for a, p in self.positions.items()
                if p.quantity > 1e-9 or abs(p.realized_pnl) > 1e-9
            },
            "total_realized_pnl": round(self.total_realized_pnl(), 4),
            "total_fees": round(self.total_fees(), 4),
            "total_equity": round(self.total_equity(), 4),
        }


class Bus:
    """In-process async pub/sub with optional Redis fan-out.

    Handlers are dispatched sequentially per-topic to avoid race conditions
    on shared mutable state. Different topics still run concurrently.

    When ``redis_url`` is provided, every ``publish()`` also pushes to a Redis
    Pub/Sub channel so other processes can subscribe. Incoming Redis messages
    are dispatched to local handlers automatically. This enables multi-worker
    deployments without changing any agent code.

    When ``redis_url`` is ``None`` (default), behaves as a pure in-process bus.
    """
    def __init__(self, redis_url: str | None = None) -> None:
        self._subs: dict[str, list[Callable[[Any], Awaitable[None]]]] = {}
        self._topic_locks: dict[str, asyncio.Lock] = {}
        self._processed_ids: set[str] = set()  # idempotency tracking
        self._pending_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks

        # Redis integration (optional)
        self._redis_url = redis_url
        self._redis_pub = None   # redis.asyncio.Redis for publishing
        self._redis_sub = None   # pubsub object for subscribing
        self._redis_listener: asyncio.Task | None = None
        self._redis_ready = False

    async def connect_redis(self) -> None:
        """Initialize Redis pub/sub if a URL was provided.

        Call once after the event loop is running. Safe to skip — the bus
        works without Redis, just in-process only.
        """
        if not self._redis_url:
            return
        try:
            import redis.asyncio as aioredis
        except ImportError:
            import logging
            logging.getLogger("bus").warning(
                "redis package not installed — falling back to in-process bus. "
                "Install with: pip install redis>=5.0")
            self._redis_url = None
            return

        import logging
        log = logging.getLogger("bus")
        try:
            self._redis_pub = aioredis.from_url(
                self._redis_url, decode_responses=True,
            )
            await self._redis_pub.ping()
            self._redis_sub = self._redis_pub.pubsub()
            self._redis_ready = True
            self._redis_listener = asyncio.create_task(self._redis_listen())
            log.info("Bus: Redis connected at %s", self._redis_url.split("@")[-1])
        except Exception as e:
            log.warning("Bus: Redis connection failed (%s) — falling back to in-process", e)
            self._redis_url = None
            self._redis_ready = False

    async def _redis_listen(self) -> None:
        """Background task: read messages from Redis and dispatch locally."""
        import json
        import logging
        log = logging.getLogger("bus")
        assert self._redis_sub is not None
        try:
            async for raw in self._redis_sub.listen():
                if raw["type"] != "message":
                    continue
                topic = raw["channel"]
                try:
                    payload = json.loads(raw["data"])
                except (json.JSONDecodeError, TypeError):
                    payload = raw["data"]
                # Skip messages we published ourselves (dedup via origin marker)
                if isinstance(payload, dict) and payload.get("_bus_origin") == id(self):
                    continue
                await self._dispatch_local(topic, payload.get("_payload", payload)
                                           if isinstance(payload, dict) else payload)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error("Redis listener died: %s", e)

    async def _dispatch_local(self, topic: str, msg: Any) -> None:
        """Dispatch to local handlers only (no Redis re-publish)."""
        lock = self._topic_locks.setdefault(topic, asyncio.Lock())
        async with lock:
            for fn in self._subs.get(topic, []):
                await self._safe(fn, msg)

    def subscribe(self, topic: str, fn: Callable[[Any], Awaitable[None]]) -> None:
        self._subs.setdefault(topic, []).append(fn)
        # Use setdefault for atomic lock creation (avoids race condition)
        self._topic_locks.setdefault(topic, asyncio.Lock())
        # Subscribe to the Redis channel too (if connected)
        if self._redis_ready and self._redis_sub is not None:
            asyncio.ensure_future(self._redis_sub.subscribe(topic))

    async def publish(self, topic: str, msg: Any) -> None:
        # Always dispatch to local handlers
        lock = self._topic_locks.setdefault(topic, asyncio.Lock())
        task = asyncio.create_task(self._dispatch(lock, topic, msg))
        self._pending_tasks.add(task)
        task.add_done_callback(self._task_done)

        # Also publish to Redis for cross-process fan-out
        if self._redis_ready and self._redis_pub is not None:
            try:
                import json
                envelope = json.dumps({
                    "_bus_origin": id(self),
                    "_payload": msg if isinstance(msg, (dict, list, str, int, float, bool, type(None))) else str(msg),
                }, default=str)
                await self._redis_pub.publish(topic, envelope)
            except Exception:
                pass  # Redis failure should never block local dispatch

    def _task_done(self, task: asyncio.Task) -> None:
        """Log unhandled exceptions from dispatch tasks and clean up."""
        self._pending_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            import logging
            logging.getLogger("bus").error(
                "Bus dispatch task failed: %s", exc, exc_info=exc)

    async def _dispatch(self, lock: asyncio.Lock, topic: str, msg: Any) -> None:
        async with lock:
            for fn in self._subs.get(topic, []):
                await self._safe(fn, msg)

    def is_duplicate(self, idempotency_key: str) -> bool:
        """Check and register an idempotency key. Returns True if already seen."""
        if idempotency_key in self._processed_ids:
            return True
        self._processed_ids.add(idempotency_key)
        # Bound the set
        if len(self._processed_ids) > 5000:
            # Keep most recent half (approximate — sets are unordered,
            # but this prevents unbounded growth)
            to_remove = list(self._processed_ids)[:2500]
            self._processed_ids -= set(to_remove)
        return False

    _HANDLER_TIMEOUT = 30.0  # seconds — kill hung handlers

    @staticmethod
    async def _safe(fn, msg):
        try:
            await asyncio.wait_for(fn(msg), timeout=Bus._HANDLER_TIMEOUT)
        except asyncio.TimeoutError:
            import logging
            logging.getLogger("bus").error(
                "handler %s.%s timed out after %.0fs",
                getattr(fn, '__module__', '?'), getattr(fn, '__qualname__', '?'),
                Bus._HANDLER_TIMEOUT)
        except Exception as e:  # pragma: no cover
            import logging
            logging.getLogger("bus").exception("handler failed: %s", e)

    async def close(self) -> None:
        """Shut down Redis connections gracefully."""
        if self._redis_listener and not self._redis_listener.done():
            self._redis_listener.cancel()
        if self._redis_sub:
            await self._redis_sub.unsubscribe()
            await self._redis_sub.close()
        if self._redis_pub:
            await self._redis_pub.close()
        self._redis_ready = False
