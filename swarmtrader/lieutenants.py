"""Lieutenants — sector aggregators that compress 122 agent signals into
6-8 concise briefings for HermesBrain.

Instead of Hermes consuming every raw signal and dumping them into one
bloated LLM prompt, each Lieutenant:
  1. Subscribes to its sector's signal.* topics
  2. Maintains a rolling window of recent signals per agent
  3. Computes a sector score (bullish/bearish/neutral + confidence)
  4. Detects intra-sector conflicts and anomalies
  5. Publishes a compressed `briefing.{sector}` every cycle
  6. Escalates urgent events via `escalation.{sector}` immediately

Result: Hermes reads 6-8 briefings (~600 tokens) instead of 122 raw
signals (~4K+ tokens).  LLM calls are faster, cheaper, and higher
signal-to-noise.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .core import Bus, Signal

log = logging.getLogger("swarm.lieutenant")

# How long a signal stays "fresh" before it's ignored
SIGNAL_TTL_S = 120.0

# ── Sector briefing payload ──────────────────────────────────────────────

@dataclass
class SectorBriefing:
    """Compressed intelligence from one sector lieutenant."""
    sector: str
    direction: str          # "bullish" | "bearish" | "neutral" | "conflicted"
    score: float            # -1.0 .. 1.0 (weighted aggregate)
    confidence: float       # 0.0 .. 1.0 (agreement level)
    n_agents: int           # how many agents reported
    n_long: int
    n_short: int
    n_flat: int
    conflict: bool          # True if agents strongly disagree
    top_signals: list[str]  # top 3 agent summaries (most confident)
    anomalies: list[str]    # unusual conditions flagged
    ts: float = field(default_factory=time.time)

    def one_liner(self) -> str:
        """Single-line summary for the Hermes brief."""
        arrow = {"bullish": "^", "bearish": "v", "neutral": "-",
                 "conflicted": "!"}[self.direction]
        conflict_flag = " CONFLICT" if self.conflict else ""
        anom = f" ANOMALY:{','.join(self.anomalies)}" if self.anomalies else ""
        return (
            f"[{self.sector}] {arrow} score={self.score:+.2f} "
            f"conf={self.confidence:.2f} "
            f"({self.n_long}L/{self.n_short}S/{self.n_flat}F "
            f"of {self.n_agents}){conflict_flag}{anom}"
        )


@dataclass
class Escalation:
    """Urgent event that bypasses the normal briefing cycle."""
    sector: str
    event_type: str     # "spike", "divergence", "cascade", "whale_dump", etc.
    severity: str       # "high", "critical"
    message: str
    related_signals: list[Signal] = field(default_factory=list)
    ts: float = field(default_factory=time.time)


# ── Cached signal snapshot ───────────────────────────────────────────────

@dataclass
class _CachedSignal:
    sig: Signal
    received_at: float


# ═════════════════════════════════════════════════════════════════════════
# Lieutenant base class
# ═════════════════════════════════════════════════════════════════════════

class Lieutenant:
    """Base class for sector aggregator lieutenants.

    Subclasses define:
      - SECTOR: str name (e.g. "momentum", "flow")
      - TOPICS: list of signal.* topics to subscribe to
      - Optional: _detect_anomalies(), _should_escalate()
    """

    SECTOR: str = "base"
    TOPICS: list[str] = []

    # Thresholds
    CONFLICT_THRESHOLD = 0.3   # if minority faction > 30%, flag conflict
    ESCALATION_SCORE_DELTA = 0.4  # score swing this big triggers escalation
    CYCLE_INTERVAL_S = 5.0     # how often to publish briefing

    def __init__(self, bus: Bus):
        self.bus = bus
        self._signals: dict[str, _CachedSignal] = {}  # key: agent_id:asset
        self._running = False
        self._prev_score: float = 0.0
        self._prev_direction: str = "neutral"
        self._briefing_count: int = 0

        for topic in self.TOPICS:
            bus.subscribe(topic, self._on_signal)

        log.info("Lieutenant [%s] online — watching %d topics",
                 self.SECTOR, len(self.TOPICS))

    async def _on_signal(self, sig: Signal):
        """Buffer incoming signal, check for immediate escalation."""
        key = f"{sig.agent_id}:{sig.asset}"
        self._signals[key] = _CachedSignal(sig=sig, received_at=time.time())

        # Check if this single signal warrants immediate escalation
        esc = self._check_instant_escalation(sig)
        if esc:
            await self.bus.publish(f"escalation.{self.SECTOR}", esc)

    def _check_instant_escalation(self, sig: Signal) -> Escalation | None:
        """Override in subclass for sector-specific instant escalation logic."""
        return None

    # ── Aggregation core ─────────────────────────────────────────────────

    def _active_signals(self) -> list[Signal]:
        """Return fresh signals only."""
        now = time.time()
        cutoff = now - SIGNAL_TTL_S
        active = []
        stale_keys = []
        for key, cached in self._signals.items():
            if cached.sig.ts > cutoff:
                active.append(cached.sig)
            else:
                stale_keys.append(key)
        # Prune stale
        for k in stale_keys:
            del self._signals[k]
        return active

    def _compute_briefing(self) -> SectorBriefing | None:
        """Aggregate active signals into a sector briefing."""
        active = self._active_signals()
        if not active:
            return None

        # Count directions
        longs = [s for s in active if s.direction == "long"]
        shorts = [s for s in active if s.direction == "short"]
        flats = [s for s in active if s.direction == "flat"]

        n = len(active)
        n_long = len(longs)
        n_short = len(shorts)
        n_flat = len(flats)

        # Weighted score: sum(strength * confidence) / sum(confidence)
        total_weight = sum(s.confidence for s in active) or 1.0
        score = sum(s.strength * s.confidence for s in active) / total_weight
        score = max(-1.0, min(1.0, score))

        # Confidence = agreement level (1.0 = unanimous, 0.0 = 50/50 split)
        if n_long + n_short > 0:
            majority = max(n_long, n_short)
            agreement = majority / (n_long + n_short)
        else:
            agreement = 1.0  # all flat = full agreement
        avg_conf = sum(s.confidence for s in active) / n

        # Direction
        if abs(score) < 0.05:
            direction = "neutral"
        elif n_long > 0 and n_short > 0 and min(n_long, n_short) / max(n_long, n_short) > self.CONFLICT_THRESHOLD:
            direction = "conflicted"
        elif score > 0:
            direction = "bullish"
        else:
            direction = "bearish"

        # Conflict detection
        conflict = direction == "conflicted"

        # Top signals (most confident, most relevant)
        top = sorted(active, key=lambda s: -s.confidence)[:3]
        top_summaries = [
            f"{s.agent_id}:{s.direction}({s.strength:+.2f},c={s.confidence:.2f})"
            for s in top
        ]

        # Sector-specific anomaly detection
        anomalies = self._detect_anomalies(active)

        return SectorBriefing(
            sector=self.SECTOR,
            direction=direction,
            score=score,
            confidence=min(1.0, agreement * avg_conf),
            n_agents=n,
            n_long=n_long,
            n_short=n_short,
            n_flat=n_flat,
            conflict=conflict,
            top_signals=top_summaries,
            anomalies=anomalies,
        )

    def _detect_anomalies(self, active: list[Signal]) -> list[str]:
        """Override in subclass for sector-specific anomaly detection."""
        return []

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def run(self):
        """Publish sector briefings on a cycle."""
        self._running = True
        log.info("Lieutenant [%s] cycle started (every %.0fs)",
                 self.SECTOR, self.CYCLE_INTERVAL_S)

        while self._running:
            try:
                briefing = self._compute_briefing()
                if briefing:
                    # Check for score swing escalation
                    score_delta = abs(briefing.score - self._prev_score)
                    if score_delta >= self.ESCALATION_SCORE_DELTA and self._briefing_count > 0:
                        esc = Escalation(
                            sector=self.SECTOR,
                            event_type="score_swing",
                            severity="high",
                            message=(
                                f"{self.SECTOR} score swung "
                                f"{self._prev_score:+.2f} -> {briefing.score:+.2f} "
                                f"(delta={score_delta:.2f})"
                            ),
                        )
                        await self.bus.publish(f"escalation.{self.SECTOR}", esc)

                    self._prev_score = briefing.score
                    self._prev_direction = briefing.direction
                    self._briefing_count += 1

                    await self.bus.publish(f"briefing.{self.SECTOR}", briefing)

            except Exception as e:
                log.exception("Lieutenant [%s] cycle error: %s", self.SECTOR, e)

            await asyncio.sleep(self.CYCLE_INTERVAL_S)

    async def stop(self):
        self._running = False

    def status(self) -> dict:
        return {
            "sector": self.SECTOR,
            "topics": len(self.TOPICS),
            "active_signals": len(self._active_signals()),
            "briefings_sent": self._briefing_count,
            "last_score": self._prev_score,
            "last_direction": self._prev_direction,
        }


# ═════════════════════════════════════════════════════════════════════════
# Concrete Lieutenants
# ═════════════════════════════════════════════════════════════════════════

class MomentumLt(Lieutenant):
    """Technical momentum and indicator signals."""
    SECTOR = "momentum"
    TOPICS = [
        "signal.momentum", "signal.mean_rev", "signal.rsi", "signal.macd",
        "signal.bollinger", "signal.vwap", "signal.ichimoku", "signal.mtf",
        "signal.atr_stop", "signal.correlation",
        "signal.filtered.momentum", "signal.filtered.rsi",
    ]

    def _detect_anomalies(self, active: list[Signal]) -> list[str]:
        anomalies = []
        # RSI extreme: if any RSI signal has strength > 0.8 or < -0.8
        for s in active:
            if s.agent_id in ("rsi", "filtered_rsi") and abs(s.strength) > 0.8:
                label = "overbought" if s.strength > 0 else "oversold"
                anomalies.append(f"RSI_{label}")
        # MACD/momentum divergence: momentum says one thing, MACD says another
        mom = next((s for s in active if s.agent_id == "momentum"), None)
        macd = next((s for s in active if s.agent_id == "macd"), None)
        if mom and macd and mom.direction != macd.direction:
            anomalies.append("mom_macd_diverge")
        return anomalies


class MarketStructureLt(Lieutenant):
    """Order book, spread, regime, liquidation, and open interest signals."""
    SECTOR = "structure"
    TOPICS = [
        "signal.orderbook", "signal.spread", "signal.regime", "signal.regime_v2",
        "signal.liquidation", "signal.liquidation_cascade", "signal.liquidation_levels",
        "signal.open_interest", "signal.prism", "signal.prism_volume",
        "signal.prism_breakout",
    ]

    def _check_instant_escalation(self, sig: Signal) -> Escalation | None:
        # Liquidation cascade = immediate escalation
        if sig.agent_id in ("liquidation_cascade", "liquidation_levels"):
            if sig.confidence > 0.7 and abs(sig.strength) > 0.6:
                return Escalation(
                    sector=self.SECTOR,
                    event_type="liquidation_cascade",
                    severity="critical",
                    message=f"Liquidation cascade detected: {sig.rationale[:100]}",
                    related_signals=[sig],
                )
        return None

    def _detect_anomalies(self, active: list[Signal]) -> list[str]:
        anomalies = []
        for s in active:
            if s.agent_id == "orderbook" and abs(s.strength) > 0.7:
                side = "bid_heavy" if s.strength > 0 else "ask_heavy"
                anomalies.append(f"book_{side}")
            if s.agent_id in ("regime", "regime_v2"):
                anomalies.append(f"regime={s.rationale[:20]}")
        return anomalies


class SentimentLt(Lieutenant):
    """Social sentiment, news, fear/greed, and narrative signals."""
    SECTOR = "sentiment"
    TOPICS = [
        "signal.fear_greed", "signal.social", "signal.social_alpha",
        "signal.social_consensus", "signal.news", "signal.rss_news",
        "signal.polymarket", "signal.narrative", "signal.sentiment_deriv",
    ]

    def _detect_anomalies(self, active: list[Signal]) -> list[str]:
        anomalies = []
        for s in active:
            if s.agent_id == "fear_greed" and abs(s.strength) > 0.8:
                label = "extreme_greed" if s.strength > 0 else "extreme_fear"
                anomalies.append(label)
            if s.agent_id == "news" and s.confidence > 0.8:
                anomalies.append("high_impact_news")
        return anomalies


class FlowLt(Lieutenant):
    """Whale movements, smart money, on-chain flow, and exchange flow signals."""
    SECTOR = "flow"
    TOPICS = [
        "signal.whale", "signal.smart_money", "signal.onchain",
        "signal.exchange_flow", "signal.stablecoin", "signal.whale_mirror",
        "signal.filtered.whale",
    ]

    def _check_instant_escalation(self, sig: Signal) -> Escalation | None:
        # Large whale movement = immediate escalation
        if sig.agent_id in ("whale", "whale_mirror", "filtered_whale"):
            if sig.confidence > 0.8 and abs(sig.strength) > 0.7:
                return Escalation(
                    sector=self.SECTOR,
                    event_type="whale_alert",
                    severity="high",
                    message=f"Major whale activity: {sig.rationale[:100]}",
                    related_signals=[sig],
                )
        return None

    def _detect_anomalies(self, active: list[Signal]) -> list[str]:
        anomalies = []
        # Exchange flow divergence from whale signal
        whale = next((s for s in active if "whale" in s.agent_id), None)
        exflow = next((s for s in active if s.agent_id == "exchange_flow"), None)
        if whale and exflow and whale.direction != exflow.direction:
            anomalies.append("whale_flow_diverge")
        for s in active:
            if s.agent_id == "stablecoin" and abs(s.strength) > 0.6:
                flow_dir = "inflow" if s.strength > 0 else "outflow"
                anomalies.append(f"stablecoin_{flow_dir}")
        return anomalies


class MacroLt(Lieutenant):
    """Funding rates, macro calendar, options/derivatives, and cross-asset signals."""
    SECTOR = "macro"
    TOPICS = [
        "signal.funding", "signal.macro", "signal.options",
        "signal.options_strategy", "signal.vol",
        "signal.confluence", "signal.birdeye", "signal.hyperliquid",
    ]

    def _detect_anomalies(self, active: list[Signal]) -> list[str]:
        anomalies = []
        for s in active:
            if s.agent_id == "funding" and abs(s.strength) > 0.6:
                label = "funding_high" if s.strength > 0 else "funding_negative"
                anomalies.append(label)
            if s.agent_id == "vol" and s.confidence > 0.7 and abs(s.strength) > 0.6:
                anomalies.append("vol_spike")
        return anomalies


class AlphaLt(Lieutenant):
    """Alpha hunting, arbitrage, MEV, sniping, and speculative strategy signals."""
    SECTOR = "alpha"
    TOPICS = [
        "signal.alpha_swarm", "signal.arbitrage", "signal.arb_live",
        "signal.sniper", "signal.mev", "signal.prediction",
        "signal.marketplace", "signal.grid", "signal.yield",
        "signal.insurance", "signal.rwa",
    ]

    def _detect_anomalies(self, active: list[Signal]) -> list[str]:
        anomalies = []
        for s in active:
            if s.agent_id in ("arbitrage", "arb_live") and s.confidence > 0.8:
                anomalies.append("arb_opportunity")
            if s.agent_id == "mev" and s.confidence > 0.7:
                anomalies.append("mev_detected")
        return anomalies


class ConsensusLt(Lieutenant):
    """Cross-cutting consensus, debate, fusion, and ML ensemble signals."""
    SECTOR = "consensus"
    TOPICS = [
        "signal.fusion", "signal.debate", "signal.consensus",
        "signal.ml", "signal.hedge", "signal.rebalance",
        "signal.position",
    ]

    def _detect_anomalies(self, active: list[Signal]) -> list[str]:
        anomalies = []
        # Debate engine disagreement = flag it
        debate = [s for s in active if s.agent_id == "debate"]
        if debate and debate[0].confidence < 0.4:
            anomalies.append("debate_deadlock")
        # Fusion low confidence = ensemble can't agree
        fusion = next((s for s in active if s.agent_id == "fusion"), None)
        if fusion and fusion.confidence < 0.3:
            anomalies.append("fusion_uncertain")
        return anomalies


# ═════════════════════════════════════════════════════════════════════════
# Registry — all lieutenants in one place
# ═════════════════════════════════════════════════════════════════════════

ALL_LIEUTENANTS = [
    MomentumLt,
    MarketStructureLt,
    SentimentLt,
    FlowLt,
    MacroLt,
    AlphaLt,
    ConsensusLt,
]

SECTOR_NAMES = [Lt.SECTOR for Lt in ALL_LIEUTENANTS]


def create_lieutenants(bus: Bus) -> list[Lieutenant]:
    """Instantiate all sector lieutenants and return them."""
    lts = [LtClass(bus) for LtClass in ALL_LIEUTENANTS]
    log.info("Created %d lieutenants: %s",
             len(lts), ", ".join(lt.SECTOR for lt in lts))
    return lts
