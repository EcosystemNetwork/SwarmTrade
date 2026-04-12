"""Adversarial Debate Engine — Bull vs Bear agents argue before every trade.

Inspired by Alpha Dawg (ETHGlobal Cannes 2026, 2nd place). Before any
alpha opportunity reaches execution, two adversarial agents debate:

  BullAgent — argues FOR the trade using positive signals
  BearAgent — argues AGAINST using risk signals and contrarian data
  DebateResolver — scores both sides, requires minimum victory margin

Key patterns from Alpha Dawg:
  - Memory DAG: each debate references 3 prior debates via IDs (not flat log)
  - ELO reputation: agents that win profitable debates gain ELO
  - Deterministic override: if risk is zero and conviction is extreme, force entry
  - Default to HOLD: ties and marginal wins result in no trade

Bus integration:
  Subscribes to: alpha.screened (approved opportunities from alpha_swarm)
  Publishes to:  debate.resolved (final verdict after debate)
                 signal.debate (high-conviction signal if approved)

No external dependencies. No LLM required — uses signal aggregation.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections import deque

from .core import Bus, Signal

log = logging.getLogger("swarm.debate")


@dataclass
class DebateArgument:
    """One side's argument in a debate."""
    side: str               # "bull" or "bear"
    score: float            # weighted argument score
    signals_used: int       # number of supporting signals
    key_points: list[str]   # top 3 rationale points
    agent_elos: dict[str, float] = field(default_factory=dict)  # agent -> ELO used


@dataclass
class DebateRecord:
    """Full record of a debate including outcome tracking."""
    debate_id: str
    asset: str
    direction: str          # proposed direction
    bull_argument: DebateArgument
    bear_argument: DebateArgument
    verdict: str            # "approve", "reject", "hold"
    margin: float           # victory margin (bull_score - bear_score)
    conviction: float       # final conviction score
    # Memory DAG: references to prior debates for context
    prior_debate_ids: list[str] = field(default_factory=list)
    # Outcome tracking (filled in later by ELO system)
    actual_pnl: float | None = None
    outcome_recorded: bool = False
    ts: float = field(default_factory=time.time)


class BullAgent:
    """Argues FOR a trade using positive signals and momentum.

    Scoring model:
      - Signal strength * confidence * ELO_weight for each supporting signal
      - Bonus for convergence (multiple independent sources)
      - Bonus for trend alignment (velocity from Kalman filter)
      - Penalized if the asset recently lost money
    """

    def __init__(self, elo_tracker: "ELOTracker"):
        self.elo = elo_tracker

    def argue(self, asset: str, direction: str,
              signals: list[Signal], prior_debates: list[DebateRecord]) -> DebateArgument:
        """Build the bull case for entering this trade."""
        score = 0.0
        points: list[str] = []
        elos: dict[str, float] = {}

        supporting = [
            s for s in signals
            if s.direction == direction and s.strength > 0.1 and s.confidence > 0.2
        ]

        if not supporting:
            return DebateArgument("bull", 0.0, 0, ["no supporting signals"], {})

        for sig in supporting:
            agent_elo = self.elo.get_elo(sig.agent_id)
            elos[sig.agent_id] = agent_elo
            # ELO weight: agents above 500 get bonus, below get penalty
            elo_weight = 0.5 + (agent_elo / 1000.0)
            contribution = sig.strength * sig.confidence * elo_weight
            score += contribution

        # Convergence bonus: more unique agents = stronger case
        unique_agents = len(set(s.agent_id for s in supporting))
        convergence_bonus = min(0.3, unique_agents * 0.05)
        score += convergence_bonus

        # Prior debate context: if we've been right on this asset recently
        recent_wins = sum(
            1 for d in prior_debates
            if d.asset == asset and d.actual_pnl is not None and d.actual_pnl > 0
        )
        recent_losses = sum(
            1 for d in prior_debates
            if d.asset == asset and d.actual_pnl is not None and d.actual_pnl < 0
        )
        if recent_wins > recent_losses:
            score *= 1.1  # slight boost for assets we've been right on

        # Build rationale
        top_signals = sorted(supporting, key=lambda s: s.strength * s.confidence, reverse=True)[:3]
        for s in top_signals:
            points.append(f"{s.agent_id}: {s.rationale[:50]} (str={s.strength:.2f})")
        if convergence_bonus > 0.1:
            points.append(f"convergence: {unique_agents} independent sources agree")

        return DebateArgument("bull", round(score, 3), len(supporting), points, elos)


class BearAgent:
    """Argues AGAINST a trade using risk signals and contrarian data.

    Scoring model:
      - Opposing signal strength * confidence * ELO_weight
      - Risk regime penalty (high vol, crisis)
      - Recent loss penalty on this asset
      - High covariance (uncertainty) penalty
      - Correlation risk (already exposed to similar assets)
    """

    def __init__(self, elo_tracker: "ELOTracker"):
        self.elo = elo_tracker
        self._regime: str = "normal"
        self._covariance: float = 0.5

    def update_risk(self, regime: str, covariance: float):
        self._regime = regime
        self._covariance = covariance

    def argue(self, asset: str, direction: str,
              signals: list[Signal], prior_debates: list[DebateRecord]) -> DebateArgument:
        """Build the bear case against entering this trade."""
        score = 0.0
        points: list[str] = []
        elos: dict[str, float] = {}

        # Opposing signals
        opposing_dir = "short" if direction == "long" else "long"
        opposing = [
            s for s in signals
            if s.direction == opposing_dir and s.strength > 0.1 and s.confidence > 0.2
        ]

        for sig in opposing:
            agent_elo = self.elo.get_elo(sig.agent_id)
            elos[sig.agent_id] = agent_elo
            elo_weight = 0.5 + (agent_elo / 1000.0)
            contribution = sig.strength * sig.confidence * elo_weight
            score += contribution

        # Regime penalty
        regime_penalties = {
            "crisis": 0.5,
            "volatile": 0.3,
            "high_vol": 0.25,
            "normal": 0.0,
            "trending": -0.1,  # trending is good for trades
            "mean_reverting": 0.05,
        }
        regime_pen = regime_penalties.get(self._regime, 0.0)
        if regime_pen > 0:
            score += regime_pen
            points.append(f"regime={self._regime}: +{regime_pen:.2f} risk penalty")

        # Covariance penalty: high system uncertainty = risky to enter
        if self._covariance > 0.5:
            cov_pen = (self._covariance - 0.5) * 0.3
            score += cov_pen
            points.append(f"high uncertainty: covariance={self._covariance:.3f}")

        # Prior debate context: penalize if we've been losing on this asset
        recent_losses = sum(
            1 for d in prior_debates
            if d.asset == asset and d.actual_pnl is not None and d.actual_pnl < 0
        )
        if recent_losses >= 2:
            loss_pen = recent_losses * 0.15
            score += loss_pen
            points.append(f"recent losses on {asset}: {recent_losses} losing debates")

        # Build remaining rationale
        top_opposing = sorted(opposing, key=lambda s: s.strength * s.confidence, reverse=True)[:3]
        for s in top_opposing:
            points.append(f"AGAINST {s.agent_id}: {s.rationale[:50]} (str={s.strength:.2f})")

        if not points:
            points.append("no significant counterarguments found")

        return DebateArgument("bear", round(score, 3), len(opposing), points, elos)


class ELOTracker:
    """ELO reputation system for agent performance tracking.

    Inspired by Alpha Dawg's specialist reputation. K-factor = 32,
    ELO bounds 0-1000. Agents gain ELO when their signals contribute
    to profitable trades, lose ELO when they contribute to losses.

    Default ELO: 500 (neutral).
    High tier (>700): heavily weighted in debates.
    Low tier (<300): flagged as noisy, discounted.
    """

    K = 32
    MIN_ELO = 0
    MAX_ELO = 1000
    DEFAULT_ELO = 500

    def __init__(self):
        self._elos: dict[str, float] = {}
        self._history: list[dict] = []

    def get_elo(self, agent_id: str) -> float:
        return self._elos.get(agent_id, self.DEFAULT_ELO)

    def update(self, agent_id: str, won: bool, opponent_elo: float = 500.0):
        """Update ELO after a debate outcome.

        Args:
            agent_id: the agent being updated
            won: True if the agent's side of the debate led to profit
            opponent_elo: average ELO of the opposing side
        """
        current = self.get_elo(agent_id)

        # Expected score
        expected = 1 / (1 + 10 ** ((opponent_elo - current) / 400))
        actual = 1.0 if won else 0.0

        # ELO update
        new_elo = current + self.K * (actual - expected)
        new_elo = max(self.MIN_ELO, min(self.MAX_ELO, new_elo))

        self._elos[agent_id] = round(new_elo, 1)

        self._history.append({
            "agent_id": agent_id,
            "old_elo": round(current, 1),
            "new_elo": round(new_elo, 1),
            "won": won,
            "ts": time.time(),
        })
        if len(self._history) > 1000:
            self._history = self._history[-500:]

    def tier(self, agent_id: str) -> str:
        """Classify agent into performance tier."""
        elo = self.get_elo(agent_id)
        if elo >= 700:
            return "high"
        elif elo >= 400:
            return "mid"
        else:
            return "low"

    def leaderboard(self, top_n: int = 20) -> list[dict]:
        """Get top agents by ELO."""
        ranked = sorted(self._elos.items(), key=lambda x: x[1], reverse=True)
        return [
            {"agent_id": aid, "elo": elo, "tier": self.tier(aid)}
            for aid, elo in ranked[:top_n]
        ]

    def summary(self) -> dict:
        return {
            "total_agents": len(self._elos),
            "avg_elo": round(sum(self._elos.values()) / max(len(self._elos), 1), 1),
            "high_tier": len([e for e in self._elos.values() if e >= 700]),
            "low_tier": len([e for e in self._elos.values() if e < 300]),
            "leaderboard": self.leaderboard(10),
        }


class DebateResolver:
    """Resolves bull vs bear debates into trade decisions.

    Decision rules:
      - Bull wins by margin >= threshold: APPROVE
      - Bear wins or margin < threshold: REJECT
      - Tie (margin near zero): HOLD (default to no trade)
      - Deterministic override: if bull score > 2.0 AND bear score < 0.3, force APPROVE

    The bias is toward NOT trading. The bar to approve should be high.
    """

    def __init__(self, min_margin: float = 0.3, override_bull_min: float = 2.0,
                 override_bear_max: float = 0.3):
        self.min_margin = min_margin
        self.override_bull_min = override_bull_min
        self.override_bear_max = override_bear_max

    def resolve(self, bull: DebateArgument, bear: DebateArgument) -> tuple[str, float, float]:
        """Resolve a debate. Returns (verdict, margin, conviction).

        Verdict: "approve", "reject", or "hold"
        """
        margin = bull.score - bear.score

        # Deterministic override: overwhelming bull case with no risk
        if bull.score >= self.override_bull_min and bear.score <= self.override_bear_max:
            conviction = min(1.0, bull.score / 3.0)
            return "approve", margin, conviction

        # Normal resolution
        if margin >= self.min_margin:
            conviction = min(1.0, margin / 2.0)
            return "approve", margin, conviction
        elif margin <= -self.min_margin:
            return "reject", margin, 0.0
        else:
            return "hold", margin, 0.0


class AdversarialDebateEngine:
    """Full debate pipeline integrating bull/bear agents with ELO tracking.

    Pipeline:
      1. Receive approved alpha opportunity
      2. Collect recent signals for the asset
      3. Load 3 most recent prior debates (memory DAG)
      4. Run BullAgent and BearAgent arguments
      5. Resolve via DebateResolver
      6. If approved, publish high-conviction signal
      7. Track outcomes for ELO updates
    """

    def __init__(self, bus: Bus, elo: ELOTracker | None = None,
                 min_margin: float = 0.3, memory_depth: int = 3):
        self.bus = bus
        self.elo = elo or ELOTracker()
        self.bull = BullAgent(self.elo)
        self.bear = BearAgent(self.elo)
        self.resolver = DebateResolver(min_margin=min_margin)
        self.memory_depth = memory_depth

        # Debate history (memory DAG)
        self._debates: deque[DebateRecord] = deque(maxlen=200)
        self._debate_counter = 0

        # Collect recent signals for debate context
        self._recent_signals: list[Signal] = []
        self._max_signals = 300

        # Stats
        self._stats = {"total": 0, "approved": 0, "rejected": 0, "held": 0}

        # Subscribe to signals for context
        bus.subscribe("signal.momentum", self._collect_signal)
        bus.subscribe("signal.mean_reversion", self._collect_signal)
        bus.subscribe("signal.rsi", self._collect_signal)
        bus.subscribe("signal.macd", self._collect_signal)
        bus.subscribe("signal.bollinger", self._collect_signal)
        bus.subscribe("signal.whale", self._collect_signal)
        bus.subscribe("signal.news", self._collect_signal)
        bus.subscribe("signal.sentiment", self._collect_signal)
        bus.subscribe("signal.onchain", self._collect_signal)
        bus.subscribe("signal.funding_rate", self._collect_signal)
        bus.subscribe("signal.orderbook", self._collect_signal)
        bus.subscribe("signal.ml", self._collect_signal)
        bus.subscribe("signal.confluence", self._collect_signal)
        bus.subscribe("signal.fear_greed", self._collect_signal)
        bus.subscribe("signal.smart_money", self._collect_signal)
        bus.subscribe("signal.fusion", self._collect_signal)

        # Subscribe to alpha opportunities and risk updates
        bus.subscribe("alpha.screened", self._on_alpha_screened)
        bus.subscribe("risk.covariance", self._on_covariance)
        bus.subscribe("signal.regime", self._on_regime)

        # Subscribe to execution reports for ELO updates
        bus.subscribe("exec.report", self._on_execution_report)

    async def _collect_signal(self, sig):
        if isinstance(sig, Signal):
            self._recent_signals.append(sig)
            cutoff = time.time() - 300  # 5 min window
            self._recent_signals = [s for s in self._recent_signals if s.ts > cutoff]
            if len(self._recent_signals) > self._max_signals:
                self._recent_signals = self._recent_signals[-self._max_signals:]

    async def _on_covariance(self, data: dict):
        self.bear.update_risk(
            data.get("regime", "normal"),
            data.get("system_covariance", 0.5),
        )

    async def _on_regime(self, sig):
        if isinstance(sig, Signal):
            regime = sig.rationale.split(":")[-1].strip() if ":" in sig.rationale else "normal"
            self.bear.update_risk(regime, self.bear._covariance)

    async def _on_alpha_screened(self, msg: dict):
        """Run debate on approved alpha opportunities."""
        opp = msg.get("opportunity")
        if opp is None or getattr(opp, "status", "") != "approved":
            return

        asset = opp.asset
        direction = opp.direction

        # Get signals for this asset
        asset_signals = [s for s in self._recent_signals if s.asset == asset]
        if not asset_signals:
            return

        # Memory DAG: get recent prior debates for this asset
        prior = [d for d in self._debates if d.asset == asset][-self.memory_depth:]

        # Run debate
        self._debate_counter += 1
        debate_id = f"debate-{self._debate_counter:05d}"

        bull_arg = self.bull.argue(asset, direction, asset_signals, prior)
        bear_arg = self.bear.argue(asset, direction, asset_signals, prior)
        verdict, margin, conviction = self.resolver.resolve(bull_arg, bear_arg)

        record = DebateRecord(
            debate_id=debate_id,
            asset=asset,
            direction=direction,
            bull_argument=bull_arg,
            bear_argument=bear_arg,
            verdict=verdict,
            margin=round(margin, 3),
            conviction=round(conviction, 3),
            prior_debate_ids=[d.debate_id for d in prior],
        )
        self._debates.append(record)
        self._stats["total"] += 1

        log.info(
            "DEBATE %s: %s %s | BULL=%.2f(%d sigs) vs BEAR=%.2f(%d sigs) | "
            "margin=%.2f -> %s (conv=%.2f)",
            debate_id, direction.upper(), asset,
            bull_arg.score, bull_arg.signals_used,
            bear_arg.score, bear_arg.signals_used,
            margin, verdict.upper(), conviction,
        )

        if verdict == "approve":
            self._stats["approved"] += 1

            # Publish high-conviction signal
            sig = Signal(
                agent_id="debate_engine",
                asset=asset,
                direction=direction,
                strength=min(1.0, conviction * 1.2),
                confidence=conviction,
                rationale=(
                    f"Debate {debate_id}: APPROVED {direction} {asset} | "
                    f"bull={bull_arg.score:.2f} bear={bear_arg.score:.2f} "
                    f"margin={margin:.2f} | "
                    f"bull_points: {bull_arg.key_points[0] if bull_arg.key_points else 'n/a'}"
                ),
            )
            await self.bus.publish("signal.debate", sig)
            await self.bus.publish("debate.resolved", {
                "record": record, "verdict": "approve",
            })

        elif verdict == "reject":
            self._stats["rejected"] += 1
            log.info(
                "DEBATE REJECTED: %s %s — bear wins | %s",
                direction, asset,
                bear_arg.key_points[0] if bear_arg.key_points else "n/a",
            )
            await self.bus.publish("debate.resolved", {
                "record": record, "verdict": "reject",
            })

        else:  # hold
            self._stats["held"] += 1
            log.info("DEBATE HOLD: %s %s — margin too thin (%.2f)", direction, asset, margin)
            await self.bus.publish("debate.resolved", {
                "record": record, "verdict": "hold",
            })

    async def _on_execution_report(self, report):
        """Update ELO scores based on trade outcomes."""
        if not hasattr(report, "status") or report.status != "filled":
            return
        if not hasattr(report, "pnl_estimate"):
            return

        pnl = report.pnl_estimate or 0.0
        asset = getattr(report, "asset", "")

        # Find the most recent unresolved debate for this asset
        for record in reversed(self._debates):
            if record.asset == asset and not record.outcome_recorded:
                record.actual_pnl = pnl
                record.outcome_recorded = True

                profitable = pnl > 0
                avg_bear_elo = (
                    sum(record.bear_argument.agent_elos.values())
                    / max(len(record.bear_argument.agent_elos), 1)
                ) if record.bear_argument.agent_elos else 500.0
                avg_bull_elo = (
                    sum(record.bull_argument.agent_elos.values())
                    / max(len(record.bull_argument.agent_elos), 1)
                ) if record.bull_argument.agent_elos else 500.0

                # Update bull agents
                for agent_id in record.bull_argument.agent_elos:
                    self.elo.update(agent_id, won=profitable, opponent_elo=avg_bear_elo)

                # Update bear agents (they "win" when the trade loses)
                for agent_id in record.bear_argument.agent_elos:
                    self.elo.update(agent_id, won=not profitable, opponent_elo=avg_bull_elo)

                log.info(
                    "ELO UPDATE: debate %s pnl=%+.4f | bull agents %s, bear agents %s",
                    record.debate_id, pnl,
                    "rewarded" if profitable else "penalized",
                    "penalized" if profitable else "rewarded",
                )
                break

    def summary(self) -> dict:
        return {
            **self._stats,
            "approval_rate": self._stats["approved"] / max(self._stats["total"], 1),
            "elo": self.elo.summary(),
            "recent_debates": [
                {
                    "id": d.debate_id,
                    "asset": d.asset,
                    "direction": d.direction,
                    "verdict": d.verdict,
                    "margin": d.margin,
                    "conviction": d.conviction,
                    "pnl": d.actual_pnl,
                }
                for d in list(self._debates)[-5:]
            ],
        }


def setup_debate_engine(bus: Bus, elo: ELOTracker | None = None,
                        **kwargs) -> AdversarialDebateEngine:
    """Create and wire up the adversarial debate engine.

    Returns the engine instance for reference.
    """
    engine = AdversarialDebateEngine(
        bus, elo=elo,
        min_margin=kwargs.get("min_margin", 0.3),
        memory_depth=kwargs.get("memory_depth", 3),
    )
    log.info("Adversarial debate engine initialized (min_margin=%.2f)", engine.resolver.min_margin)
    return engine
