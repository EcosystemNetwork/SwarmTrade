"""Demo replay mode — pre-recorded market scenario for presentations.

Generates a realistic multi-phase market scenario that showcases:
1. Accumulation (sideways with slight upward bias)
2. Breakout rally (strong momentum, high confidence signals)
3. Volatility spike (whipsaw, circuit breaker tests)
4. Mean reversion (recovery after dip)
5. Trend continuation (steady climb)

This gives a compelling demo without needing live API keys.
"""
from __future__ import annotations
import asyncio, math, random, time
from .core import Bus, MarketSnapshot

# Seed for reproducible demo
_SEED = 42


class DemoScout:
    """Replays a scripted multi-phase market scenario."""

    def __init__(self, bus: Bus, symbol: str = "ETH",
                 start_price: float = 2200.0, interval: float = 0.3):
        self.bus = bus
        self.symbol = symbol
        self.price = start_price
        self.interval = interval
        self._stop = False
        self._rng = random.Random(_SEED)
        self._tick = 0

    def stop(self):
        self._stop = True

    async def run(self):
        while not self._stop:
            phase = self._get_phase()
            drift, vol = phase["drift"], phase["vol"]

            # GBM step with phase-specific parameters
            shock = self._rng.gauss(0, 1)
            ret = drift + vol * shock
            self.price *= math.exp(ret)

            # Occasional realistic spikes
            if self._rng.random() < 0.02:
                spike = self._rng.choice([-1, 1]) * self._rng.uniform(0.005, 0.015)
                self.price *= (1 + spike)

            # Floor at $100
            self.price = max(100.0, self.price)

            await self.bus.publish("market.snapshot", MarketSnapshot(
                ts=time.time(),
                prices={self.symbol: round(self.price, 2)},
                gas_gwei=self._rng.uniform(8, 50),
            ))

            self._tick += 1
            await asyncio.sleep(self.interval)

    def _get_phase(self) -> dict:
        """Return drift/vol parameters based on current tick (phase of demo)."""
        t = self._tick

        if t < 100:
            # Phase 1: Accumulation — low vol sideways
            return {"drift": 0.0001, "vol": 0.002, "name": "accumulation"}
        elif t < 200:
            # Phase 2: Breakout rally — strong uptrend
            return {"drift": 0.0008, "vol": 0.003, "name": "breakout"}
        elif t < 280:
            # Phase 3: Volatility spike — high vol, slight down
            return {"drift": -0.0003, "vol": 0.008, "name": "volatility"}
        elif t < 380:
            # Phase 4: Recovery / mean reversion
            return {"drift": 0.0004, "vol": 0.004, "name": "recovery"}
        elif t < 500:
            # Phase 5: Trend continuation
            return {"drift": 0.0005, "vol": 0.003, "name": "trend"}
        else:
            # Phase 6: Late session — tightening range
            return {"drift": 0.0001, "vol": 0.002, "name": "consolidation"}


class MultiAssetDemoScout:
    """Demo scout that generates correlated prices for multiple assets."""

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 0.3):
        self.bus = bus
        self.interval = interval
        self._stop = False
        self._rng = random.Random(_SEED)
        self._tick = 0

        # Default asset configs
        configs = {
            "ETH": {"start": 2200.0, "beta": 1.0},
            "BTC": {"start": 65000.0, "beta": 0.85},
            "SOL": {"start": 145.0, "beta": 1.3},
        }
        self.assets = assets or ["ETH", "BTC", "SOL"]
        self.prices = {}
        self.betas = {}
        for a in self.assets:
            cfg = configs.get(a, {"start": 100.0, "beta": 1.0})
            self.prices[a] = cfg["start"]
            self.betas[a] = cfg["beta"]

    def stop(self):
        self._stop = True

    async def run(self):
        while not self._stop:
            phase = self._get_phase()
            market_shock = self._rng.gauss(0, 1)

            prices = {}
            for asset in self.assets:
                beta = self.betas[asset]
                idio = self._rng.gauss(0, 1) * 0.3  # Idiosyncratic component
                combined = beta * market_shock + idio
                ret = phase["drift"] * beta + phase["vol"] * combined
                self.prices[asset] *= math.exp(ret)
                self.prices[asset] = max(1.0, self.prices[asset])
                prices[asset] = round(self.prices[asset], 2)

            await self.bus.publish("market.snapshot", MarketSnapshot(
                ts=time.time(),
                prices=prices,
                gas_gwei=self._rng.uniform(8, 50),
            ))

            self._tick += 1
            await asyncio.sleep(self.interval)

    def _get_phase(self) -> dict:
        t = self._tick
        if t < 100:
            return {"drift": 0.0001, "vol": 0.002}
        elif t < 200:
            return {"drift": 0.0008, "vol": 0.003}
        elif t < 280:
            return {"drift": -0.0003, "vol": 0.008}
        elif t < 380:
            return {"drift": 0.0004, "vol": 0.004}
        elif t < 500:
            return {"drift": 0.0005, "vol": 0.003}
        else:
            return {"drift": 0.0001, "vol": 0.002}
