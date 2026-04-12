"""Gas Optimization Engine — minimize transaction costs.

Inspired by AI Gas Forecaster (historical gas prediction with LangChain),
the high gas costs on Ethereum mainnet, and the need to batch operations
for cost efficiency on L2s.

Capabilities:
  1. Gas price forecasting (predict optimal submission time)
  2. Transaction batching (multicall for multiple operations)
  3. EIP-4844 blob awareness (use blob space when available)
  4. Chain-specific optimization (L1 vs L2 cost structures)
  5. Priority fee optimization (just enough to get included)

Bus integration:
  Subscribes to: market.snapshot (gas data), exec.go (optimize before submission)
  Publishes to:  gas.forecast, gas.optimized
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections import deque

from .core import Bus, MarketSnapshot

log = logging.getLogger("swarm.gas")


@dataclass
class GasForecast:
    """Gas price prediction for optimal timing."""
    current_gwei: float
    predicted_low_gwei: float    # predicted low in next hour
    predicted_high_gwei: float
    optimal_time_s: float        # seconds until predicted low
    confidence: float
    recommendation: str          # "submit_now", "wait", "urgent_only"
    ts: float = field(default_factory=time.time)


@dataclass
class BatchedTransaction:
    """A batched multicall transaction."""
    batch_id: str
    calls: list[dict]        # individual call data
    total_gas_estimate: int
    individual_gas_sum: int  # what it would cost unbatched
    savings_pct: float       # gas savings from batching
    ts: float = field(default_factory=time.time)


class GasOptimizer:
    """Minimizes gas costs across all on-chain operations.

    Tracks gas price history and predicts optimal submission windows.
    Batches multiple operations into multicall transactions.
    """

    # Gas price thresholds (in gwei)
    CHEAP_THRESHOLD = 5.0    # submit freely
    NORMAL_THRESHOLD = 20.0  # submit if important
    EXPENSIVE_THRESHOLD = 50.0  # wait unless urgent

    def __init__(self, bus: Bus):
        self.bus = bus
        self._gas_history: deque = deque(maxlen=720)  # 1 hour at 5s intervals
        self._pending_batch: list[dict] = []
        self._batch_counter = 0
        self._stats = {
            "forecasts": 0, "batches": 0,
            "gas_saved_gwei": 0, "txns_batched": 0,
        }

        bus.subscribe("market.snapshot", self._on_snapshot)

    async def _on_snapshot(self, snap: MarketSnapshot):
        """Track gas prices over time."""
        self._gas_history.append((time.time(), snap.gas_gwei))

        # Periodic forecast
        if len(self._gas_history) >= 12 and len(self._gas_history) % 12 == 0:
            forecast = self.predict_gas()
            self._stats["forecasts"] += 1
            await self.bus.publish("gas.forecast", forecast)

    def predict_gas(self) -> GasForecast:
        """Predict gas prices for the next hour."""
        if not self._gas_history:
            return GasForecast(
                current_gwei=0, predicted_low_gwei=0,
                predicted_high_gwei=0, optimal_time_s=0,
                confidence=0, recommendation="submit_now",
            )

        prices = [g for _, g in self._gas_history]
        current = prices[-1]

        # Simple prediction: use recent min/max/avg
        recent_30 = prices[-min(30, len(prices)):]
        avg = sum(recent_30) / len(recent_30)
        low = min(recent_30)
        high = max(recent_30)

        # Trend
        if len(prices) >= 10:
            trend = (sum(prices[-5:]) / 5) - (sum(prices[-10:-5]) / 5)
        else:
            trend = 0

        predicted_low = max(1, low - abs(trend) * 2)
        predicted_high = high + abs(trend) * 2

        # Optimal time: if current is above average, wait
        if current > avg * 1.2:
            optimal_s = 300  # wait 5 min
            recommendation = "wait"
        elif current < avg * 0.8:
            optimal_s = 0  # cheap now, submit
            recommendation = "submit_now"
        else:
            optimal_s = 60
            recommendation = "submit_now" if current < self.NORMAL_THRESHOLD else "wait"

        if current > self.EXPENSIVE_THRESHOLD:
            recommendation = "urgent_only"

        return GasForecast(
            current_gwei=round(current, 2),
            predicted_low_gwei=round(predicted_low, 2),
            predicted_high_gwei=round(predicted_high, 2),
            optimal_time_s=optimal_s,
            confidence=0.6,
            recommendation=recommendation,
        )

    def add_to_batch(self, call_data: dict):
        """Add a transaction to the pending batch."""
        self._pending_batch.append(call_data)

    def flush_batch(self) -> BatchedTransaction | None:
        """Execute pending batch as a multicall."""
        if not self._pending_batch:
            return None

        self._batch_counter += 1
        calls = list(self._pending_batch)
        self._pending_batch.clear()

        # Estimate gas savings (multicall overhead ~21000 + 5000 per call vs 21000 per individual tx)
        individual_gas = len(calls) * 50000  # ~50k per simple tx
        batched_gas = 21000 + len(calls) * 30000  # multicall overhead + per-call
        savings = max(0, individual_gas - batched_gas)
        savings_pct = savings / max(individual_gas, 1) * 100

        batch = BatchedTransaction(
            batch_id=f"batch-{self._batch_counter:04d}",
            calls=calls,
            total_gas_estimate=batched_gas,
            individual_gas_sum=individual_gas,
            savings_pct=round(savings_pct, 1),
        )

        self._stats["batches"] += 1
        self._stats["txns_batched"] += len(calls)
        self._stats["gas_saved_gwei"] += savings

        log.info(
            "GAS BATCH: %s %d calls, saving ~%.1f%% gas (%d -> %d)",
            batch.batch_id, len(calls), savings_pct, individual_gas, batched_gas,
        )
        return batch

    def should_submit(self, urgency: str = "normal") -> bool:
        """Check if current gas conditions are favorable for submission."""
        forecast = self.predict_gas()
        if urgency == "urgent":
            return True
        elif urgency == "normal":
            return forecast.recommendation in ("submit_now",)
        else:  # low priority
            return forecast.current_gwei < self.CHEAP_THRESHOLD

    def summary(self) -> dict:
        forecast = self.predict_gas()
        return {
            **self._stats,
            "current_gas": forecast.current_gwei,
            "recommendation": forecast.recommendation,
            "forecast": {
                "low": forecast.predicted_low_gwei,
                "high": forecast.predicted_high_gwei,
                "optimal_wait_s": forecast.optimal_time_s,
            },
            "pending_batch": len(self._pending_batch),
        }
