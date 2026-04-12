"""Multi-timeframe momentum: aligns short, medium, and long period trends
to produce high-conviction directional signals."""
from __future__ import annotations
import logging
from collections import deque
from .core import Bus, MarketSnapshot, Signal

log = logging.getLogger("swarm.multitf")


class MultiTimeframeMomentum:
    """Tracks momentum across three timeframes and fires strong signals
    only when all three align.

    Timeframes (in ticks, not wall-clock — adapts to poll rate):
        short  =  10 ticks  (~20s at 2s poll)
        medium =  50 ticks  (~100s)
        long   = 200 ticks  (~400s)

    Publishes signal.mtf with strength proportional to alignment quality.
    """

    name = "mtf"

    def __init__(self, bus: Bus, asset: str = "ETH",
                 short: int = 10, medium: int = 50, long: int = 200):
        self.bus = bus
        self.asset = asset
        self.short_w = short
        self.medium_w = medium
        self.long_w = long
        self.prices: deque[float] = deque(maxlen=long + 1)
        bus.subscribe("market.snapshot", self._on_snap)

    async def _on_snap(self, snap: MarketSnapshot):
        price = snap.prices.get(self.asset)
        if price is None:
            return
        self.prices.append(price)
        if len(self.prices) <= self.long_w:
            return

        prices = list(self.prices)
        s_ret = (prices[-1] / prices[-self.short_w] - 1)
        m_ret = (prices[-1] / prices[-self.medium_w] - 1)
        l_ret = (prices[-1] / prices[-self.long_w] - 1)

        # Normalize returns to [-1, 1] range (cap at ±10% move)
        s_norm = max(-1.0, min(1.0, s_ret / 0.10))
        m_norm = max(-1.0, min(1.0, m_ret / 0.10))
        l_norm = max(-1.0, min(1.0, l_ret / 0.10))

        # Check alignment: all three must agree on direction
        signs = [
            1 if s_norm > 0.01 else (-1 if s_norm < -0.01 else 0),
            1 if m_norm > 0.01 else (-1 if m_norm < -0.01 else 0),
            1 if l_norm > 0.01 else (-1 if l_norm < -0.01 else 0),
        ]
        non_zero = [s for s in signs if s != 0]
        if len(non_zero) < 2:
            return  # not enough directional conviction

        alignment = sum(non_zero) / len(non_zero)  # -1 to +1
        if abs(alignment) < 0.5:
            return  # mixed signals, skip

        # Strength = weighted average of normalized returns
        strength = 0.2 * s_norm + 0.3 * m_norm + 0.5 * l_norm
        strength = max(-1.0, min(1.0, strength))

        # Confidence based on alignment quality + trend consistency
        all_aligned = abs(alignment) == 1.0
        confidence = 0.8 if all_aligned else 0.5

        # Boost confidence if all timeframes show accelerating trend
        if abs(s_norm) > abs(m_norm) > abs(l_norm) * 0.5:
            confidence = min(1.0, confidence + 0.15)

        if abs(strength) < 0.05:
            return

        sig = Signal(
            self.name, self.asset,
            "long" if strength > 0 else "short",
            strength, confidence,
            f"s={s_ret:+.4f} m={m_ret:+.4f} l={l_ret:+.4f} align={alignment:+.2f}",
        )
        await self.bus.publish("signal.mtf", sig)
