"""Scouts (data) and analysts (signals)."""
from __future__ import annotations
import asyncio, math, random, time
from collections import deque
from .core import Bus, MarketSnapshot, Signal


class MockScout:
    """Geometric Brownian motion price feed. Replace with RPC/subgraph in prod."""
    def __init__(self, bus: Bus, symbol: str = "ETH", start: float = 2000.0,
                 vol: float = 0.004, interval: float = 0.2):
        self.bus, self.symbol, self.price = bus, symbol, start
        self.vol, self.interval = vol, interval
        self._stop = False

    def stop(self): self._stop = True

    async def run(self):
        while not self._stop:
            self.price *= math.exp(random.gauss(0, self.vol))
            await self.bus.publish("market.snapshot", MarketSnapshot(
                ts=time.time(),
                prices={self.symbol: self.price},
                gas_gwei=random.uniform(8, 40),
            ))
            await asyncio.sleep(self.interval)


class _WindowAnalyst:
    name: str = "base"
    window: int = 20

    def __init__(self, bus: Bus, asset: str = "ETH"):
        self.bus, self.asset = bus, asset
        self.buf: deque[float] = deque(maxlen=self.window)
        bus.subscribe("market.snapshot", self._on)

    async def _on(self, snap: MarketSnapshot):
        if self.asset not in snap.prices: return
        price = snap.prices[self.asset]
        # Reject non-finite or non-positive prices
        if not isinstance(price, (int, float)) or price != price or price <= 0 or price == float('inf'):
            return
        self.buf.append(price)
        if len(self.buf) < self.window: return
        sig = self.compute()
        if sig is not None:
            await self.bus.publish(f"signal.{self.name}", sig)

    def compute(self) -> Signal | None:  # override
        raise NotImplementedError


class MomentumAnalyst(_WindowAnalyst):
    name = "momentum"
    window = 20

    def compute(self):
        ret = (self.buf[-1] / self.buf[0]) - 1
        return Signal(self.name, self.asset,
                      "long" if ret > 0 else "short" if ret < 0 else "flat",
                      max(-1.0, min(1.0, ret * 80)),
                      min(1.0, abs(ret) * 120),
                      f"window_return={ret:+.4f}")


class MeanReversionAnalyst(_WindowAnalyst):
    name = "mean_rev"
    window = 50

    def compute(self):
        mean = sum(self.buf) / len(self.buf)
        var = sum((p - mean) ** 2 for p in self.buf) / len(self.buf)
        sd = max(1e-9, var ** 0.5)
        z = (self.buf[-1] - mean) / sd
        return Signal(self.name, self.asset,
                      "short" if z > 0 else "long" if z < 0 else "flat",
                      max(-1.0, min(1.0, -z / 2)),
                      min(1.0, abs(z) / 2),
                      f"z={z:+.3f}")


class VolatilityAnalyst(_WindowAnalyst):
    """Confidence damper: high vol => lower confidence elsewhere via signal.vol."""
    name = "vol"
    window = 30

    def compute(self):
        rets = [self.buf[i] / self.buf[i-1] - 1 for i in range(1, len(self.buf))]
        mean = sum(rets) / len(rets)
        sd = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
        return Signal(self.name, self.asset, "flat", 0.0,
                      min(1.0, sd * 100), f"sd={sd:.5f}")
