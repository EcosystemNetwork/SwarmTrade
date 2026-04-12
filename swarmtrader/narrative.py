"""Event Correlation Narrative Engine — transform raw events into stories.

Inspired by GhostFi (event correlation → market narratives for AI),
Meme Sentinels (concurrent processing of 1000+ tokens), and CryptoDaily
Brief (blending on-chain and off-chain data into personalized insights).

Raw blockchain events (pool swaps, large transfers, liquidations) are
meaningless individually. This engine correlates them into narratives:
  "Whale accumulated 500 ETH over 3 hours while funding rates went negative
   and social sentiment shifted bullish — classic accumulation pattern."

The narrative becomes context for the AI Brain and the Debate Engine,
replacing raw signal numbers with human-readable market stories.

Bus integration:
  Subscribes to: signal.*, intelligence.*, fusion.signal, fusion.conflict
  Publishes to:  narrative.update, signal.narrative

No external dependencies.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections import defaultdict

from .core import Bus, Signal

log = logging.getLogger("swarm.narrative")


@dataclass
class MarketEvent:
    """A notable market event detected from signal data."""
    event_id: str
    event_type: str         # "whale_move", "funding_shift", "sentiment_flip", etc.
    asset: str
    magnitude: float        # how significant (0-1)
    description: str
    source_agent: str
    ts: float = field(default_factory=time.time)


@dataclass
class MarketNarrative:
    """A correlated market narrative combining multiple events."""
    narrative_id: str
    asset: str
    headline: str           # one-line summary
    body: str               # full narrative
    events: list[MarketEvent]
    # Analysis
    direction_bias: str     # "bullish", "bearish", "neutral", "conflicted"
    conviction: float       # 0-1
    pattern: str            # "accumulation", "distribution", "breakout", etc.
    # Timeframe
    timespan_minutes: float
    ts: float = field(default_factory=time.time)


# Event detection thresholds
EVENT_THRESHOLDS = {
    "whale_move": {"min_strength": 0.6, "agents": ["whale", "smart_money"]},
    "funding_shift": {"min_strength": 0.4, "agents": ["funding_rate"]},
    "sentiment_flip": {"min_strength": 0.5, "agents": ["sentiment", "social", "news"]},
    "volume_surge": {"min_strength": 0.5, "agents": ["orderbook", "exchange_flow"]},
    "liquidation_cascade": {"min_strength": 0.7, "agents": ["liquidation", "liquidation_cascade"]},
    "divergence": {"min_strength": 0.4, "agents": ["correlation", "onchain"]},
    "technical_break": {"min_strength": 0.6, "agents": ["confluence", "rsi", "bollinger"]},
    "fear_shift": {"min_strength": 0.5, "agents": ["fear_greed"]},
    "ml_signal": {"min_strength": 0.6, "agents": ["ml"]},
}

# Known narrative patterns (event combinations → pattern name)
NARRATIVE_PATTERNS = {
    frozenset(["whale_move", "sentiment_flip"]): {
        "pattern": "accumulation",
        "template": "Smart money accumulating {asset} while sentiment turns {direction}",
    },
    frozenset(["whale_move", "volume_surge"]): {
        "pattern": "institutional_entry",
        "template": "Large players entering {asset} with significant volume",
    },
    frozenset(["liquidation_cascade", "volume_surge"]): {
        "pattern": "cascade_event",
        "template": "Liquidation cascade in {asset} triggering volume spike",
    },
    frozenset(["technical_break", "volume_surge"]): {
        "pattern": "breakout",
        "template": "Technical breakout in {asset} confirmed by volume",
    },
    frozenset(["sentiment_flip", "fear_shift"]): {
        "pattern": "regime_change",
        "template": "Market sentiment regime shift detected for {asset}",
    },
    frozenset(["funding_shift", "whale_move"]): {
        "pattern": "positioning",
        "template": "Funding rate shift coinciding with whale positioning in {asset}",
    },
    frozenset(["divergence", "technical_break"]): {
        "pattern": "divergence_break",
        "template": "Price-indicator divergence resolving with technical break in {asset}",
    },
}


class NarrativeEngine:
    """Transforms raw market events into correlated narratives.

    Pipeline:
      1. Collect signals → detect notable events
      2. Group events by asset and time window
      3. Match event combinations against known patterns
      4. Generate narrative text
      5. Publish for AI Brain and Debate Engine consumption
    """

    def __init__(self, bus: Bus, window_s: float = 300.0,
                 min_events_for_narrative: int = 2):
        self.bus = bus
        self.window_s = window_s
        self.min_events = min_events_for_narrative
        self._events: dict[str, list[MarketEvent]] = defaultdict(list)
        self._narratives: list[MarketNarrative] = []
        self._event_counter = 0
        self._narrative_counter = 0
        self._stats = {"events": 0, "narratives": 0}

        # Subscribe to all relevant signal topics
        for topic in [
            "signal.whale", "signal.smart_money", "signal.funding_rate",
            "signal.sentiment", "signal.social", "signal.news",
            "signal.orderbook", "signal.exchange_flow",
            "signal.liquidation", "signal.liquidation_cascade",
            "signal.correlation", "signal.onchain",
            "signal.confluence", "signal.rsi", "signal.bollinger",
            "signal.fear_greed", "signal.ml",
            "fusion.signal", "fusion.conflict",
        ]:
            bus.subscribe(topic, self._on_signal)

    async def _on_signal(self, sig):
        """Detect notable events from incoming signals."""
        if isinstance(sig, Signal):
            await self._detect_event(sig)
        elif isinstance(sig, dict) and "asset" in sig:
            # FusedSignal or conflict data
            pass

    async def _detect_event(self, sig: Signal):
        """Check if a signal qualifies as a notable event."""
        for event_type, config in EVENT_THRESHOLDS.items():
            if sig.agent_id in config["agents"] and sig.strength >= config["min_strength"]:
                self._event_counter += 1
                event = MarketEvent(
                    event_id=f"evt-{self._event_counter:05d}",
                    event_type=event_type,
                    asset=sig.asset,
                    magnitude=sig.strength,
                    description=sig.rationale[:100],
                    source_agent=sig.agent_id,
                )
                self._events[sig.asset].append(event)
                self._stats["events"] += 1

                # Trim old events
                cutoff = time.time() - self.window_s
                self._events[sig.asset] = [
                    e for e in self._events[sig.asset] if e.ts > cutoff
                ]

                # Check for narrative formation
                await self._try_narrative(sig.asset)
                break  # one event per signal

    async def _try_narrative(self, asset: str):
        """Check if current events form a recognizable narrative."""
        events = self._events.get(asset, [])
        if len(events) < self.min_events:
            return

        # Get unique event types in current window
        event_types = set(e.event_type for e in events)

        # Match against known patterns
        best_match = None
        best_match_size = 0
        for pattern_key, pattern_info in NARRATIVE_PATTERNS.items():
            overlap = event_types & pattern_key
            if len(overlap) >= 2 and len(overlap) > best_match_size:
                best_match = pattern_info
                best_match_size = len(overlap)

        if not best_match:
            # No known pattern — generate generic narrative
            if len(event_types) >= self.min_events:
                best_match = {
                    "pattern": "multi_signal",
                    "template": "Multiple significant events detected for {asset}",
                }
            else:
                return

        # Don't generate duplicate narratives too fast
        recent = [n for n in self._narratives if n.asset == asset and time.time() - n.ts < 60]
        if recent:
            return

        # Determine direction bias
        long_events = sum(1 for e in events if any(
            e.event_type in ["whale_move", "sentiment_flip", "volume_surge"]
            and e.magnitude > 0.5 for _ in [1]
        ))
        short_events = len(events) - long_events
        if long_events > short_events:
            direction = "bullish"
        elif short_events > long_events:
            direction = "bearish"
        else:
            direction = "conflicted"

        # Build narrative
        self._narrative_counter += 1
        headline = best_match["template"].format(asset=asset, direction=direction)
        body_parts = [f"[{e.event_type}] {e.description}" for e in events[-5:]]

        timespan = max(e.ts for e in events) - min(e.ts for e in events)

        narrative = MarketNarrative(
            narrative_id=f"narr-{self._narrative_counter:05d}",
            asset=asset,
            headline=headline,
            body=" | ".join(body_parts),
            events=list(events[-5:]),
            direction_bias=direction,
            conviction=min(1.0, len(events) * 0.15),
            pattern=best_match["pattern"],
            timespan_minutes=round(timespan / 60, 1),
        )
        self._narratives.append(narrative)
        if len(self._narratives) > 200:
            self._narratives = self._narratives[-100:]
        self._stats["narratives"] += 1

        log.info(
            "NARRATIVE: %s — %s | %s (%d events, %.0fmin span)",
            narrative.narrative_id, headline, direction,
            len(events), narrative.timespan_minutes,
        )

        await self.bus.publish("narrative.update", narrative)

        # Publish as signal for Strategist
        sig = Signal(
            agent_id="narrative",
            asset=asset,
            direction="long" if direction == "bullish" else "short" if direction == "bearish" else "flat",
            strength=narrative.conviction,
            confidence=min(1.0, len(event_types) * 0.2),
            rationale=headline,
        )
        await self.bus.publish("signal.narrative", sig)

    def summary(self) -> dict:
        return {
            **self._stats,
            "active_assets": len(self._events),
            "recent_narratives": [
                {
                    "id": n.narrative_id,
                    "asset": n.asset,
                    "headline": n.headline,
                    "pattern": n.pattern,
                    "direction": n.direction_bias,
                    "events": len(n.events),
                }
                for n in self._narratives[-5:]
            ],
        }
