"""Data Fusion Pipeline — cross-source convergence scoring.

Inspired by CookFi AI (multi-source data fusion), Meme Sentinels (concurrent
token processing), and velox (agent-per-concern with LLM coordinator).

Collects signals from all agents within time windows, cross-references
independent sources for convergence patterns, and outputs enriched
FusedSignal events with convergence scores.

Key patterns:
  - Convergence scoring: more independent sources agreeing = higher conviction
  - Conflict detection: flag when sources strongly disagree
  - Tiered update frequency: high-ranked assets get faster cycles
  - Source independence weighting: correlated sources count less

Bus integration:
  Subscribes to: signal.* (all raw signal topics)
  Publishes to:  fusion.signal (enriched convergence signals)
                 fusion.conflict (disagreement alerts)

No external dependencies.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from collections import defaultdict

from .core import Bus, Signal

log = logging.getLogger("swarm.fusion")

# Source categories for independence weighting
# Signals from the same category are partially correlated
SOURCE_CATEGORIES = {
    # Technical Analysis (correlated with each other)
    "rsi": "technical", "macd": "technical", "bollinger": "technical",
    "vwap": "technical", "ichimoku": "technical", "confluence": "technical",
    "multitf": "technical", "atr_trailing": "technical",
    # Momentum / Trend
    "momentum": "trend", "mean_reversion": "trend",
    # On-chain
    "onchain": "onchain", "whale": "onchain", "smart_money": "onchain",
    "exchange_flow": "onchain",
    # Sentiment / Social
    "sentiment": "sentiment", "social": "sentiment", "news": "sentiment",
    "fear_greed": "sentiment",
    # Market Microstructure
    "orderbook": "microstructure", "funding_rate": "microstructure",
    "spread": "microstructure", "open_interest": "microstructure",
    # ML / Statistical
    "ml": "quantitative", "correlation": "quantitative",
    # Special
    "liquidation": "risk", "liquidation_cascade": "risk",
    "arbitrage": "opportunity", "polymarket": "prediction",
    "regime": "regime", "options": "derivatives",
}

# Independence discount: signals from same category count as this fraction
CATEGORY_DISCOUNT = 0.5


@dataclass
class FusedSignal:
    """Enriched signal with cross-source convergence metrics."""
    asset: str
    direction: str
    # Convergence metrics
    convergence_score: float      # 0-1, higher = more sources agree
    source_count: int             # total unique sources
    independent_count: float      # category-adjusted independent source count
    avg_strength: float
    avg_confidence: float
    # Conflict metrics
    conflict_ratio: float         # 0-1, ratio of disagreeing sources
    strongest_bull: str           # agent with strongest long signal
    strongest_bear: str           # agent with strongest short signal
    # Supporting data
    supporting_agents: list[str]
    opposing_agents: list[str]
    rationale: str
    ts: float = field(default_factory=time.time)


@dataclass
class AssetBucket:
    """Collects signals for a single asset within a time window."""
    asset: str
    signals: list[Signal] = field(default_factory=list)
    last_fusion: float = 0.0
    rank: int = 0  # higher rank = more frequent updates


class DataFusionPipeline:
    """Cross-references independent signal sources for convergence scoring.

    Collects all signals within a configurable time window, groups by asset,
    and computes convergence scores that measure how many independent sources
    agree on direction.

    Tiered update frequency:
      - Rank 1 (top assets by signal volume): fuse every 30s
      - Rank 2: fuse every 60s
      - Rank 3 (low activity): fuse every 120s
    """

    def __init__(self, bus: Bus, window_s: float = 120.0,
                 min_sources: int = 2, fusion_interval: float = 30.0):
        self.bus = bus
        self.window_s = window_s
        self.min_sources = min_sources
        self.fusion_interval = fusion_interval
        self._buckets: dict[str, AssetBucket] = {}
        self._max_signals_per_asset = 100
        self._fusion_count = 0
        self._conflict_count = 0

        # Subscribe to all signal topics
        signal_topics = [
            "signal.momentum", "signal.mean_reversion", "signal.volatility",
            "signal.rsi", "signal.macd", "signal.bollinger", "signal.vwap",
            "signal.ichimoku", "signal.smart_money", "signal.whale",
            "signal.sentiment", "signal.news", "signal.polymarket",
            "signal.funding_rate", "signal.orderbook",
            "signal.regime", "signal.ml", "signal.confluence",
            "signal.liquidation_cascade", "signal.atr_trailing",
            "signal.fear_greed", "signal.onchain", "signal.arbitrage",
            "signal.correlation", "signal.multitf",
            "signal.exchange_flow", "signal.stablecoin", "signal.macro",
            "signal.options", "signal.social", "signal.open_interest",
        ]
        for topic in signal_topics:
            bus.subscribe(topic, self._on_signal)

    def _get_bucket(self, asset: str) -> AssetBucket:
        if asset not in self._buckets:
            self._buckets[asset] = AssetBucket(asset=asset)
        return self._buckets[asset]

    async def _on_signal(self, sig: Signal):
        """Collect signal into asset bucket and trigger fusion if ready."""
        if not isinstance(sig, Signal):
            return

        bucket = self._get_bucket(sig.asset)
        bucket.signals.append(sig)

        # Trim old signals
        cutoff = time.time() - self.window_s
        bucket.signals = [s for s in bucket.signals if s.ts > cutoff]
        if len(bucket.signals) > self._max_signals_per_asset:
            bucket.signals = bucket.signals[-self._max_signals_per_asset:]

        # Update rank based on signal volume
        bucket.rank = min(3, len(bucket.signals) // 5 + 1)

        # Check if fusion is due
        interval = self._tier_interval(bucket.rank)
        if time.time() - bucket.last_fusion >= interval:
            await self._fuse(bucket)

    def _tier_interval(self, rank: int) -> float:
        """Get fusion interval based on asset rank."""
        if rank >= 3:
            return self.fusion_interval         # 30s for high-activity
        elif rank == 2:
            return self.fusion_interval * 2     # 60s for medium
        else:
            return self.fusion_interval * 4     # 120s for low

    async def _fuse(self, bucket: AssetBucket):
        """Compute convergence score for all signals in a bucket."""
        bucket.last_fusion = time.time()

        if len(bucket.signals) < self.min_sources:
            return

        # Deduplicate by agent (keep latest per agent)
        latest_by_agent: dict[str, Signal] = {}
        for sig in bucket.signals:
            if sig.agent_id not in latest_by_agent or sig.ts > latest_by_agent[sig.agent_id].ts:
                latest_by_agent[sig.agent_id] = sig

        signals = list(latest_by_agent.values())
        if len(signals) < self.min_sources:
            return

        # Classify by direction
        bulls: list[Signal] = []
        bears: list[Signal] = []
        for s in signals:
            weighted = s.strength * s.confidence
            if s.direction == "long" and weighted > 0.05:
                bulls.append(s)
            elif s.direction == "short" and weighted > 0.05:
                bears.append(s)

        total = len(bulls) + len(bears)
        if total == 0:
            return

        # Determine dominant direction
        bull_weight = sum(s.strength * s.confidence for s in bulls)
        bear_weight = sum(s.strength * s.confidence for s in bears)

        if bull_weight > bear_weight:
            direction = "long"
            supporting = bulls
            opposing = bears
        elif bear_weight > bull_weight:
            direction = "short"
            supporting = bears
            opposing = bulls
        else:
            return  # Dead even — no signal

        # Compute category-adjusted independent source count
        categories_seen: dict[str, int] = defaultdict(int)
        independent_count = 0.0
        for s in supporting:
            cat = SOURCE_CATEGORIES.get(s.agent_id, "other")
            if categories_seen[cat] == 0:
                independent_count += 1.0
            else:
                independent_count += CATEGORY_DISCOUNT
            categories_seen[cat] += 1

        # Convergence score: independent sources / total possible categories
        max_categories = len(set(SOURCE_CATEGORIES.values()))
        convergence = min(1.0, independent_count / max(max_categories * 0.3, 1))

        # Conflict ratio
        conflict_ratio = len(opposing) / max(total, 1)

        # Average metrics for supporting signals
        avg_strength = sum(s.strength for s in supporting) / max(len(supporting), 1)
        avg_confidence = sum(s.confidence for s in supporting) / max(len(supporting), 1)

        # Find strongest bull and bear
        strongest_bull = max(bulls, key=lambda s: s.strength * s.confidence).agent_id if bulls else ""
        strongest_bear = max(bears, key=lambda s: s.strength * s.confidence).agent_id if bears else ""

        fused = FusedSignal(
            asset=bucket.asset,
            direction=direction,
            convergence_score=round(convergence, 3),
            source_count=len(supporting),
            independent_count=round(independent_count, 1),
            avg_strength=round(avg_strength, 3),
            avg_confidence=round(avg_confidence, 3),
            conflict_ratio=round(conflict_ratio, 3),
            strongest_bull=strongest_bull,
            strongest_bear=strongest_bear,
            supporting_agents=[s.agent_id for s in supporting],
            opposing_agents=[s.agent_id for s in opposing],
            rationale=(
                f"Fusion: {len(supporting)} sources ({independent_count:.1f} independent) "
                f"agree {direction} on {bucket.asset} | "
                f"convergence={convergence:.2f} conflict={conflict_ratio:.2f} | "
                f"top: {', '.join(s.agent_id for s in supporting[:3])}"
            ),
        )

        self._fusion_count += 1

        # Publish fused signal
        await self.bus.publish("fusion.signal", fused)

        # Also publish as a regular signal for Strategist consumption
        convergence_signal = Signal(
            agent_id="fusion",
            asset=bucket.asset,
            direction=direction,
            strength=min(1.0, avg_strength * convergence),
            confidence=min(1.0, avg_confidence * (1 - conflict_ratio * 0.5)),
            rationale=fused.rationale,
        )
        await self.bus.publish("signal.fusion", convergence_signal)

        # Check for significant conflict
        if conflict_ratio > 0.4 and len(opposing) >= 2:
            self._conflict_count += 1
            log.warning(
                "FUSION CONFLICT on %s: %d sources say %s but %d disagree (ratio=%.2f)",
                bucket.asset, len(supporting), direction, len(opposing), conflict_ratio,
            )
            await self.bus.publish("fusion.conflict", {
                "asset": bucket.asset,
                "dominant_direction": direction,
                "supporting_count": len(supporting),
                "opposing_count": len(opposing),
                "conflict_ratio": conflict_ratio,
                "ts": time.time(),
            })

        if self._fusion_count % 10 == 0:
            log.info(
                "Fusion #%d: %s %s convergence=%.2f sources=%d independent=%.1f conflict=%.2f",
                self._fusion_count, direction.upper(), bucket.asset,
                convergence, len(supporting), independent_count, conflict_ratio,
            )

    def summary(self) -> dict:
        return {
            "assets_tracked": len(self._buckets),
            "total_fusions": self._fusion_count,
            "total_conflicts": self._conflict_count,
            "buckets": {
                asset: {
                    "signals": len(b.signals),
                    "rank": b.rank,
                    "last_fusion": round(time.time() - b.last_fusion, 1) if b.last_fusion else None,
                }
                for asset, b in self._buckets.items()
                if b.signals
            },
        }
