"""TWAP execution: splits large orders into smaller slices over time
to reduce market impact and improve average fill price."""
from __future__ import annotations
import asyncio, logging, time, uuid
from dataclasses import dataclass, field
from .core import Bus, TradeIntent

log = logging.getLogger("swarm.twap")


@dataclass
class TWAPOrder:
    """Tracks a TWAP order being executed in slices."""
    original_intent: TradeIntent
    total_amount: float
    n_slices: int
    interval_s: float
    slices_sent: int = 0
    slices_filled: int = 0
    total_filled_value: float = 0.0
    start_ts: float = field(default_factory=time.time)
    completed: bool = False


class TWAPExecutor:
    """Intercepts large trade intents and splits them into time-weighted
    slices to minimize market impact.

    Flow:
      1. Subscribes to intent.new
      2. If amount > threshold, absorbs the intent and creates N smaller
         child intents spaced evenly over the execution window
      3. Small intents pass through unchanged

    Publishes child intents back to intent.new (downstream risk/execution
    sees individual slices).
    """

    name = "twap"

    def __init__(self, bus: Bus, threshold: float = 1000.0,
                 n_slices: int = 5, window_s: float = 60.0):
        self.bus = bus
        self.threshold = threshold
        self.n_slices = n_slices
        self.window_s = window_s
        self.active_orders: dict[str, TWAPOrder] = {}
        self._stop = False
        self._processing = False

        bus.subscribe("intent.new", self._on_intent)
        bus.subscribe("exec.report", self._on_report)

    def stop(self):
        self._stop = True

    async def _on_intent(self, intent: TradeIntent):
        # Avoid intercepting our own child slices
        if hasattr(intent, '_twap_child'):
            return

        if intent.amount_in < self.threshold:
            return  # let small orders through

        # Absorb and split
        order = TWAPOrder(
            original_intent=intent,
            total_amount=intent.amount_in,
            n_slices=self.n_slices,
            interval_s=self.window_s / self.n_slices,
        )
        self.active_orders[intent.id] = order
        log.info("TWAP splitting %s: $%.2f into %d slices over %.0fs",
                 intent.id, intent.amount_in, self.n_slices, self.window_s)

        # Launch slice execution in background
        asyncio.create_task(self._execute_slices(order))

    async def _execute_slices(self, order: TWAPOrder):
        slice_amount = order.total_amount / order.n_slices
        orig = order.original_intent

        for i in range(order.n_slices):
            if self._stop:
                break

            child = TradeIntent.new(
                asset_in=orig.asset_in,
                asset_out=orig.asset_out,
                amount_in=slice_amount,
                min_out=0,
                ttl=order.interval_s * 2,  # generous TTL per slice
                supporting=orig.supporting,
            )
            child._twap_child = True  # type: ignore[attr-defined]
            child._twap_parent = orig.id  # type: ignore[attr-defined]

            order.slices_sent += 1
            log.info("TWAP slice %d/%d for %s: $%.2f",
                     i + 1, order.n_slices, orig.id, slice_amount)

            await self.bus.publish("intent.new", child)

            if i < order.n_slices - 1:
                await asyncio.sleep(order.interval_s)

        order.completed = True

    async def _on_report(self, report):
        if report.status != "filled":
            return
        # Track fills for active TWAP orders
        for order_id, order in list(self.active_orders.items()):
            if order.completed and order.slices_sent == order.slices_filled:
                avg_price = (order.total_filled_value / order.slices_filled
                             if order.slices_filled > 0 else 0)
                log.info("TWAP %s complete: %d/%d filled, avg_price=%.2f",
                         order_id, order.slices_filled, order.n_slices, avg_price)
                del self.active_orders[order_id]

    def status(self) -> dict:
        return {
            "active_orders": len(self.active_orders),
            "orders": {
                oid: {
                    "total": o.total_amount,
                    "slices_sent": o.slices_sent,
                    "slices_filled": o.slices_filled,
                    "completed": o.completed,
                }
                for oid, o in self.active_orders.items()
            },
        }
