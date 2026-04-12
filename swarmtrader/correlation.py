"""Cross-asset correlation agent: detects when BTC leads altcoin moves,
pair divergences, and correlation regime shifts."""
from __future__ import annotations
import logging, math
from collections import deque
from .core import Bus, MarketSnapshot, Signal

log = logging.getLogger("swarm.correlation")


class CorrelationAgent:
    """Tracks rolling correlation between a reference asset (BTC) and target
    assets. Produces signals when:

    1. **Lead-lag**: BTC moves sharply and the target hasn't followed yet
       → anticipate the target catching up.
    2. **Divergence**: Correlation breaks down (target decouples from BTC)
       → reduce confidence / flag regime change.
    3. **Correlation spike**: Assets re-correlate after a divergence
       → mean-reversion opportunity.

    Publishes signal.correlation per target asset.
    """

    name = "correlation"

    def __init__(self, bus: Bus, reference: str = "BTC",
                 targets: list[str] | None = None, window: int = 60):
        self.bus = bus
        self.reference = reference
        self.targets = targets or ["ETH"]
        self.window = window
        self._prices: dict[str, deque[float]] = {
            reference: deque(maxlen=window + 1),
        }
        for t in self.targets:
            self._prices[t] = deque(maxlen=window + 1)
        bus.subscribe("market.snapshot", self._on_snap)

    async def _on_snap(self, snap: MarketSnapshot):
        ref_price = snap.prices.get(self.reference)
        if ref_price is None:
            return
        self._prices[self.reference].append(ref_price)

        for target in self.targets:
            tgt_price = snap.prices.get(target)
            if tgt_price is None:
                continue
            self._prices[target].append(tgt_price)
            if len(self._prices[target]) < self.window:
                continue

            ref_returns = _returns(list(self._prices[self.reference]))
            tgt_returns = _returns(list(self._prices[target]))
            n = min(len(ref_returns), len(tgt_returns))
            if n < 20:
                continue

            ref_r = ref_returns[-n:]
            tgt_r = tgt_returns[-n:]

            corr = _pearson(ref_r, tgt_r)

            # --- Lead-lag detection ---
            # Check if BTC moved recently but target lagged
            ref_recent = sum(ref_r[-5:])  # last 5 ticks
            tgt_recent = sum(tgt_r[-5:])
            lag_gap = ref_recent - tgt_recent

            strength = 0.0
            rationale_parts = [f"corr={corr:.3f}"]

            if abs(corr) > 0.5 and abs(lag_gap) > 0.005:
                # High correlation + lag gap = target should catch up
                strength = max(-1.0, min(1.0, lag_gap * 20))
                rationale_parts.append(f"lag_gap={lag_gap:+.4f}")

            # --- Divergence detection ---
            if abs(corr) < 0.2 and abs(ref_recent) > 0.01:
                # Correlation breakdown while reference is moving
                # Reduce signal strength but flag it
                strength *= 0.3
                rationale_parts.append("DIVERGENCE")

            # --- Correlation regime shift ---
            if len(ref_r) >= 40:
                old_corr = _pearson(ref_r[:20], tgt_r[:20])
                new_corr = _pearson(ref_r[-20:], tgt_r[-20:])
                corr_shift = new_corr - old_corr
                if abs(corr_shift) > 0.3:
                    rationale_parts.append(f"corr_shift={corr_shift:+.3f}")
                    # Strengthening correlation after divergence = catch-up trade
                    if corr_shift > 0.3 and abs(lag_gap) > 0.003:
                        strength = max(-1.0, min(1.0, strength + lag_gap * 10))

            if abs(strength) < 0.05:
                continue

            confidence = min(1.0, abs(corr) * 0.6 + 0.3)

            sig = Signal(
                self.name, target,
                "long" if strength > 0 else "short",
                strength, confidence,
                " ".join(rationale_parts),
            )
            await self.bus.publish("signal.correlation", sig)


def _returns(prices: list[float]) -> list[float]:
    return [(prices[i] / prices[i - 1]) - 1 for i in range(1, len(prices))]


def _pearson(x: list[float], y: list[float]) -> float:
    n = len(x)
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    sx = math.sqrt(max(1e-18, sum((xi - mx) ** 2 for xi in x)))
    sy = math.sqrt(max(1e-18, sum((yi - my) ** 2 for yi in y)))
    return cov / (sx * sy)
