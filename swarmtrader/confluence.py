"""Confluence detector: identifies when multiple independent signal sources
agree, producing high-conviction composite signals."""
from __future__ import annotations
import logging, time
from collections import defaultdict
from .core import Bus, Signal

log = logging.getLogger("swarm.confluence")

# Signal groups — signals within same group are correlated, across groups are independent
SIGNAL_GROUPS = {
    "price_action": {"momentum", "mean_rev", "mtf"},
    "microstructure": {"orderbook", "spread", "funding"},
    "external": {"prism", "prism_breakout", "prism_vol", "news"},
    "cross_asset": {"correlation"},
    "regime": {"regime"},
}


class ConfluenceDetector:
    """Monitors all signal.* events and fires signal.confluence when
    independent signal groups agree on direction.

    Independence matters: 3 price-action signals agreeing is weaker than
    price-action + microstructure + external agreeing, because the latter
    represents genuinely uncorrelated information sources.

    Publishes signal.confluence with:
      - strength: amplified by number of agreeing independent groups
      - confidence: scaled by cross-group agreement quality
    """

    name = "confluence"

    def __init__(self, bus: Bus, window_s: float = 30.0,
                 min_groups: int = 2):
        self.bus = bus
        self.window_s = window_s
        self.min_groups = min_groups

        # Recent signals: {asset: [(Signal, group_name, timestamp)]}
        self._recent: dict[str, list[tuple[Signal, str, float]]] = defaultdict(list)

        # Build reverse lookup: agent_id → group
        self._agent_to_group: dict[str, str] = {}
        for group, agents in SIGNAL_GROUPS.items():
            for agent in agents:
                self._agent_to_group[agent] = group

        # Subscribe to all signal topics
        for topic in [
            "signal.momentum", "signal.mean_rev", "signal.mtf",
            "signal.orderbook", "signal.spread", "signal.funding",
            "signal.prism", "signal.prism_breakout", "signal.prism_volume",
            "signal.news", "signal.correlation", "signal.regime",
        ]:
            bus.subscribe(topic, self._on_signal)

    async def _on_signal(self, sig: Signal):
        group = self._agent_to_group.get(sig.agent_id, "unknown")
        if group == "unknown":
            return

        now = time.time()
        self._recent[sig.asset].append((sig, group, now))

        # Prune old signals
        cutoff = now - self.window_s
        self._recent[sig.asset] = [
            (s, g, t) for s, g, t in self._recent[sig.asset] if t > cutoff
        ]

        # Analyze confluence
        await self._check_confluence(sig.asset)

    async def _check_confluence(self, asset: str):
        entries = self._recent.get(asset, [])
        if not entries:
            return

        # Get latest signal per group
        group_signals: dict[str, Signal] = {}
        for sig, group, ts in entries:
            # Keep latest per group
            if group not in group_signals or ts > 0:
                group_signals[group] = sig

        if len(group_signals) < self.min_groups:
            return

        # Categorize groups by direction
        bullish_groups: list[str] = []
        bearish_groups: list[str] = []
        bull_strength = 0.0
        bear_strength = 0.0

        for group, sig in group_signals.items():
            if group == "regime":
                continue  # regime is directionally neutral
            if sig.direction == "long" and sig.strength > 0.05:
                bullish_groups.append(group)
                bull_strength += abs(sig.strength) * sig.confidence
            elif sig.direction == "short" and sig.strength < -0.05:
                bearish_groups.append(group)
                bear_strength += abs(sig.strength) * sig.confidence

        # Need min_groups independent groups agreeing
        if len(bullish_groups) >= self.min_groups:
            n = len(bullish_groups)
            # Amplify: more independent groups = stronger signal
            raw_strength = bull_strength / n
            amplified = min(1.0, raw_strength * (1 + 0.3 * (n - 1)))
            confidence = min(1.0, 0.5 + 0.15 * n)

            sig = Signal(
                self.name, asset, "long",
                amplified, confidence,
                f"confluence={n}groups [{','.join(bullish_groups)}]",
            )
            await self.bus.publish("signal.confluence", sig)

        elif len(bearish_groups) >= self.min_groups:
            n = len(bearish_groups)
            raw_strength = bear_strength / n
            amplified = min(1.0, raw_strength * (1 + 0.3 * (n - 1)))
            confidence = min(1.0, 0.5 + 0.15 * n)

            sig = Signal(
                self.name, asset, "short",
                -amplified, confidence,
                f"confluence={n}groups [{','.join(bearish_groups)}]",
            )
            await self.bus.publish("signal.confluence", sig)
