"""Core data types and async pub/sub bus."""
from __future__ import annotations
import asyncio, time, uuid
from dataclasses import dataclass, field
from typing import Literal, Callable, Awaitable, Any

Direction = Literal["long", "short", "flat"]


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


class Bus:
    """Tiny in-process async pub/sub. Swap for NATS/Redis in prod."""
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[[Any], Awaitable[None]]]] = {}

    def subscribe(self, topic: str, fn: Callable[[Any], Awaitable[None]]) -> None:
        self._subs.setdefault(topic, []).append(fn)

    async def publish(self, topic: str, msg: Any) -> None:
        for fn in self._subs.get(topic, []):
            asyncio.create_task(self._safe(fn, msg))

    @staticmethod
    async def _safe(fn, msg):
        try:
            await fn(msg)
        except Exception as e:  # pragma: no cover
            import logging
            logging.getLogger("bus").exception("handler failed: %s", e)
