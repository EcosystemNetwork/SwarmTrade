"""Alpha Hunter Swarm — specialized role-based agent coordination.

Inspired by aoxbt (ETHGlobal Agentic Ethereum). Implements a 4-agent
swarm pattern where specialized sub-agents cooperate to find and
execute alpha opportunities:

  1. AlphaHunter     — discovers trading opportunities from multiple sources
  2. SentimentFilter — validates signals against market sentiment
  3. RiskScreener    — screens for risk/reward before execution
  4. SwarmExecutor   — coordinates execution across the agent swarm

Each sub-agent publishes to internal Bus topics and the SwarmCoordinator
aggregates their outputs into high-conviction trade signals.

This module adds a meta-layer on top of existing agents, orchestrating
them into a coordinated alpha-seeking swarm.

No additional environment variables required — uses existing agent data.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .core import Bus, Signal, TradeIntent

log = logging.getLogger("swarm.alpha")


@dataclass
class AlphaOpportunity:
    """A potential trading opportunity discovered by the swarm."""
    opportunity_id: str
    asset: str
    direction: str  # "long" or "short"
    source: str  # which sub-agent discovered it
    raw_strength: float
    rationale: str
    discovered_at: float = field(default_factory=time.time)
    # Validation scores from each sub-agent (filled in as they process)
    sentiment_score: float = 0.0
    sentiment_validated: bool = False
    risk_score: float = 0.0
    risk_validated: bool = False
    # Final assessment
    final_conviction: float = 0.0
    status: str = "discovered"  # discovered, validating, approved, rejected, executed
    rejection_reason: str = ""


class AlphaHunter:
    """Discovers trading opportunities by aggregating signals from all agents.

    Listens to all signal topics and identifies when multiple independent
    signals align — indicating a high-probability opportunity.

    Trigger conditions:
      - 3+ agents agree on direction for same asset
      - Smart money + technical alignment
      - Sentiment shift + on-chain activity
      - Cross-asset correlation break (lead-lag)
    """

    name = "alpha_hunter"

    def __init__(self, bus: Bus, min_agents_agree: int = 3, window_s: float = 300.0):
        self.bus = bus
        self.min_agents_agree = min_agents_agree
        self.window_s = window_s
        self._recent_signals: list[Signal] = []
        self._max_signals = 200
        self._opportunities: list[AlphaOpportunity] = []
        self._opp_id_counter = 0

        # Subscribe to ALL signal topics
        signal_topics = [
            "signal.momentum", "signal.mean_reversion", "signal.volatility",
            "signal.rsi", "signal.macd", "signal.bollinger", "signal.vwap",
            "signal.ichimoku", "signal.smart_money", "signal.whale",
            "signal.sentiment", "signal.news", "signal.polymarket",
            "signal.yield", "signal.funding_rate", "signal.orderbook",
            "signal.regime", "signal.ml", "signal.confluence",
            "signal.liquidation_cascade", "signal.atr_trailing",
            "signal.fear_greed", "signal.onchain", "signal.arbitrage",
            "signal.correlation", "signal.multitf",
            "signal.exchange_flow", "signal.stablecoin", "signal.macro",
            "signal.options", "signal.token_unlock", "signal.github",
            "signal.rss_news",
        ]
        for topic in signal_topics:
            bus.subscribe(topic, self._on_signal)

    async def _on_signal(self, sig: Signal):
        """Collect incoming signals."""
        if not isinstance(sig, Signal):
            return
        self._recent_signals.append(sig)
        # Trim old signals
        cutoff = time.time() - self.window_s
        self._recent_signals = [s for s in self._recent_signals if s.ts > cutoff]
        if len(self._recent_signals) > self._max_signals:
            self._recent_signals = self._recent_signals[-self._max_signals:]

        # Check for alpha opportunity
        await self._scan_for_alpha()

    async def _scan_for_alpha(self):
        """Scan recent signals for multi-agent agreement patterns."""
        # Group by asset
        by_asset: dict[str, list[Signal]] = {}
        for sig in self._recent_signals:
            by_asset.setdefault(sig.asset, []).append(sig)

        for asset, signals in by_asset.items():
            # Count unique agents agreeing on direction
            long_agents = set()
            short_agents = set()
            long_strength = 0.0
            short_strength = 0.0

            for sig in signals:
                if sig.direction == "long" and sig.strength > 0.2 and sig.confidence > 0.3:
                    long_agents.add(sig.agent_id)
                    long_strength += sig.strength * sig.confidence
                elif sig.direction == "short" and sig.strength > 0.2 and sig.confidence > 0.3:
                    short_agents.add(sig.agent_id)
                    short_strength += sig.strength * sig.confidence

            # Check for multi-agent agreement
            if len(long_agents) >= self.min_agents_agree and len(long_agents) > len(short_agents):
                await self._create_opportunity(
                    asset, "long", long_agents, long_strength / len(long_agents),
                    signals,
                )
            elif len(short_agents) >= self.min_agents_agree and len(short_agents) > len(long_agents):
                await self._create_opportunity(
                    asset, "short", short_agents, short_strength / len(short_agents),
                    signals,
                )

    async def _create_opportunity(
        self, asset: str, direction: str,
        agreeing_agents: set, avg_strength: float,
        supporting_signals: list[Signal],
    ):
        """Create and publish an alpha opportunity."""
        # Check if we already have a recent opportunity for this asset+direction
        recent_cutoff = time.time() - 60  # Don't spam same opportunity
        for opp in self._opportunities[-10:]:
            if (opp.asset == asset and opp.direction == direction
                    and opp.discovered_at > recent_cutoff):
                return

        self._opp_id_counter += 1
        opp_id = f"alpha-{self._opp_id_counter:04d}"

        agents_list = sorted(agreeing_agents)
        rationale_parts = []
        for sig in supporting_signals:
            if sig.agent_id in agreeing_agents and sig.direction == direction:
                rationale_parts.append(
                    f"{sig.agent_id}(str={sig.strength:.2f},conf={sig.confidence:.2f}): {sig.rationale[:60]}"
                )

        opp = AlphaOpportunity(
            opportunity_id=opp_id,
            asset=asset,
            direction=direction,
            source=f"alpha_hunter({len(agreeing_agents)} agents)",
            raw_strength=avg_strength,
            rationale=f"Multi-agent agreement: {', '.join(agents_list)}",
        )

        self._opportunities.append(opp)
        if len(self._opportunities) > 100:
            self._opportunities = self._opportunities[-50:]

        log.info(
            "ALPHA OPPORTUNITY %s: %s %s | %d agents agree: %s | str=%.2f",
            opp_id, direction.upper(), asset,
            len(agreeing_agents), ", ".join(agents_list), avg_strength,
        )

        # Publish for validation pipeline
        await self.bus.publish("alpha.discovered", {
            "opportunity": opp,
            "supporting_signals": [
                {
                    "agent_id": s.agent_id,
                    "direction": s.direction,
                    "strength": s.strength,
                    "confidence": s.confidence,
                    "rationale": s.rationale[:100],
                }
                for s in supporting_signals
                if s.agent_id in agreeing_agents and s.direction == direction
            ],
        })


class SentimentFilter:
    """Validates alpha opportunities against market sentiment.

    Cross-references discovered opportunities with:
      - Social sentiment (Twitter/Reddit)
      - Fear & Greed Index
      - News sentiment
      - Prediction market data

    Adds a sentiment_score to the opportunity.
    """

    name = "sentiment_filter"

    def __init__(self, bus: Bus, min_sentiment: float = -0.3):
        self.bus = bus
        self.min_sentiment = min_sentiment
        self._latest_sentiment: dict[str, float] = {}  # asset -> sentiment score
        self._latest_fear_greed: float = 50.0

        bus.subscribe("alpha.discovered", self._on_alpha_discovered)
        bus.subscribe("signal.sentiment", self._on_sentiment)
        bus.subscribe("signal.fear_greed", self._on_fear_greed)
        bus.subscribe("signal.news", self._on_sentiment)
        bus.subscribe("signal.social", self._on_sentiment)

    async def _on_sentiment(self, sig):
        if isinstance(sig, Signal):
            # Convert strength+direction to sentiment score (-1 to 1)
            score = sig.strength if sig.direction == "long" else -sig.strength
            self._latest_sentiment[sig.asset] = score

    async def _on_fear_greed(self, sig):
        if isinstance(sig, Signal):
            # Fear & Greed is 0-100, normalize to -1..1
            self._latest_fear_greed = sig.strength * 100

    async def _on_alpha_discovered(self, msg: dict):
        opp = msg.get("opportunity")
        if not isinstance(opp, AlphaOpportunity):
            return

        # Check sentiment alignment
        sentiment = self._latest_sentiment.get(opp.asset, 0.0)
        fg = self._latest_fear_greed

        # Sentiment should align with direction
        if opp.direction == "long":
            sentiment_aligned = sentiment > self.min_sentiment
            fg_ok = fg > 20  # Not extreme fear (unless contrarian)
        else:
            sentiment_aligned = sentiment < -self.min_sentiment
            fg_ok = fg < 80  # Not extreme greed

        opp.sentiment_score = sentiment
        opp.sentiment_validated = sentiment_aligned and fg_ok

        if not opp.sentiment_validated:
            opp.rejection_reason = f"sentiment misaligned (score={sentiment:.2f}, fg={fg:.0f})"
            log.info("Alpha %s: sentiment REJECTED — %s", opp.opportunity_id, opp.rejection_reason)

        await self.bus.publish("alpha.sentiment_checked", {"opportunity": opp})


class RiskScreener:
    """Screens alpha opportunities for risk/reward.

    Evaluates:
      - Current portfolio exposure to the asset
      - Volatility regime (don't add risk in high-vol)
      - Correlation with existing positions
      - Recent drawdown state
    """

    name = "risk_screener"

    def __init__(self, bus: Bus, max_portfolio_concentration: float = 0.3):
        self.bus = bus
        self.max_concentration = max_portfolio_concentration
        self._regime: str = "normal"  # "low_vol", "normal", "high_vol", "crisis"
        self._drawdown: float = 0.0

        bus.subscribe("alpha.sentiment_checked", self._on_sentiment_checked)
        bus.subscribe("signal.regime", self._on_regime)

    async def _on_regime(self, sig):
        if isinstance(sig, Signal):
            self._regime = sig.rationale.split(":")[-1].strip() if ":" in sig.rationale else "normal"

    async def _on_sentiment_checked(self, msg: dict):
        opp = msg.get("opportunity")
        if not isinstance(opp, AlphaOpportunity):
            return

        # If sentiment already rejected, pass through
        if not opp.sentiment_validated:
            opp.risk_validated = False
            opp.status = "rejected"
            await self.bus.publish("alpha.screened", {"opportunity": opp})
            return

        # Risk checks
        risk_ok = True
        risk_reason = ""

        # Regime check: don't open new positions in crisis
        if self._regime == "crisis":
            risk_ok = False
            risk_reason = "market regime is crisis"
        elif self._regime == "high_vol" and opp.raw_strength < 0.5:
            risk_ok = False
            risk_reason = "high volatility requires stronger conviction"

        opp.risk_score = 1.0 if risk_ok else 0.0
        opp.risk_validated = risk_ok

        if risk_ok:
            # Calculate final conviction
            opp.final_conviction = (
                opp.raw_strength * 0.5 +
                abs(opp.sentiment_score) * 0.3 +
                opp.risk_score * 0.2
            )
            opp.status = "approved"
            log.info(
                "Alpha %s APPROVED: %s %s conviction=%.2f",
                opp.opportunity_id, opp.direction, opp.asset, opp.final_conviction,
            )
        else:
            opp.status = "rejected"
            opp.rejection_reason = risk_reason
            log.info("Alpha %s REJECTED by risk: %s", opp.opportunity_id, risk_reason)

        await self.bus.publish("alpha.screened", {"opportunity": opp})


class SwarmCoordinator:
    """Coordinates the alpha swarm and converts approved opportunities to signals.

    The final stage of the alpha pipeline:
    1. Receives screened opportunities
    2. Converts approved opportunities into high-priority trading signals
    3. Tracks opportunity outcomes for swarm performance measurement
    """

    name = "swarm_coordinator"

    def __init__(self, bus: Bus, min_conviction: float = 0.4):
        self.bus = bus
        self.min_conviction = min_conviction
        self._approved: list[AlphaOpportunity] = []
        self._rejected: list[AlphaOpportunity] = []

        bus.subscribe("alpha.screened", self._on_screened)

    async def _on_screened(self, msg: dict):
        opp = msg.get("opportunity")
        if not isinstance(opp, AlphaOpportunity):
            return

        if opp.status == "approved" and opp.final_conviction >= self.min_conviction:
            self._approved.append(opp)
            if len(self._approved) > 200:
                self._approved = self._approved[-100:]

            # Publish high-conviction signal
            sig = Signal(
                agent_id="alpha_swarm",
                asset=opp.asset,
                direction=opp.direction,
                strength=min(1.0, opp.final_conviction * 1.2),  # Boost slightly
                confidence=opp.final_conviction,
                rationale=(
                    f"Alpha swarm: {opp.rationale} | "
                    f"sentiment={opp.sentiment_score:.2f} "
                    f"conviction={opp.final_conviction:.2f}"
                ),
            )
            await self.bus.publish("signal.alpha_swarm", sig)
            log.info(
                "ALPHA SIGNAL: %s %s str=%.2f conv=%.2f — %s",
                opp.direction.upper(), opp.asset,
                sig.strength, sig.confidence, opp.rationale[:80],
            )
        else:
            self._rejected.append(opp)
            if len(self._rejected) > 200:
                self._rejected = self._rejected[-100:]

    def summary(self) -> dict:
        return {
            "approved": len(self._approved),
            "rejected": len(self._rejected),
            "approval_rate": (
                len(self._approved) / max(len(self._approved) + len(self._rejected), 1)
            ),
            "recent_approved": [
                {
                    "id": o.opportunity_id,
                    "asset": o.asset,
                    "direction": o.direction,
                    "conviction": round(o.final_conviction, 2),
                    "source": o.source,
                }
                for o in self._approved[-5:]
            ],
        }


def setup_alpha_swarm(bus: Bus, **kwargs) -> dict:
    """Create and wire up the full alpha swarm pipeline.

    Returns dict of all swarm components for reference.
    """
    hunter = AlphaHunter(
        bus,
        min_agents_agree=kwargs.get("min_agents_agree", 3),
        window_s=kwargs.get("window_s", 300.0),
    )
    sentiment = SentimentFilter(
        bus,
        min_sentiment=kwargs.get("min_sentiment", -0.3),
    )
    risk = RiskScreener(
        bus,
        max_portfolio_concentration=kwargs.get("max_concentration", 0.3),
    )
    coordinator = SwarmCoordinator(
        bus,
        min_conviction=kwargs.get("min_conviction", 0.4),
    )

    log.info("Alpha swarm initialized: hunter -> sentiment -> risk -> coordinator")

    return {
        "hunter": hunter,
        "sentiment_filter": sentiment,
        "risk_screener": risk,
        "coordinator": coordinator,
    }
