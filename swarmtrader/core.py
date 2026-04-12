"""Core data types, position management, and async pub/sub bus."""
from __future__ import annotations
import asyncio, time, uuid
from dataclasses import dataclass, field
from typing import Literal, Callable, Awaitable, Any

Direction = Literal["long", "short", "flat"]

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
class TradeIntent:
    id: str
    asset_in: str
    asset_out: str
    amount_in: float
    min_out: float
    ttl: float
    supporting: list[Signal] = field(default_factory=list)

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
    status: Literal["filled", "rejected", "expired", "error"]
    tx_hash: str | None
    fill_price: float | None
    realized_slippage: float | None
    pnl_estimate: float | None
    note: str = ""
    side: str = ""          # "buy" or "sell"
    quantity: float = 0.0   # amount of base asset
    asset: str = ""         # base asset symbol (e.g. "ETH")
    fee_usd: float = 0.0   # fee paid in USD


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
    """Tiny in-process async pub/sub. Swap for NATS/Redis in prod.

    Handlers are dispatched sequentially per-topic to avoid race conditions
    on shared mutable state. Different topics still run concurrently.
    """
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[Any], Awaitable[None]]]] = {}
        self._topic_locks: dict[str, asyncio.Lock] = {}
        self._processed_ids: set[str] = set()  # idempotency tracking

    def subscribe(self, topic: str, fn: Callable[[Any], Awaitable[None]]) -> None:
        self._subs.setdefault(topic, []).append(fn)
        if topic not in self._topic_locks:
            self._topic_locks[topic] = asyncio.Lock()

    async def publish(self, topic: str, msg: Any) -> None:
        lock = self._topic_locks.get(topic)
        if lock is None:
            lock = asyncio.Lock()
            self._topic_locks[topic] = lock
        asyncio.create_task(self._dispatch(lock, topic, msg))

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

    @staticmethod
    async def _safe(fn, msg):
        try:
            await fn(msg)
        except Exception as e:  # pragma: no cover
            import logging
            logging.getLogger("bus").exception("handler failed: %s", e)
