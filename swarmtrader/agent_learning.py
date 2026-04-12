"""Enhanced Autonomous Agent Learning System.

Inspired by Moon Dev's Agent-Zero (persistent memory, multi-agent cooperation,
learning from experience). Extends the existing AgentMemory system with:

  - Reinforcement-style learning from trade outcomes
  - Strategy weight adaptation based on PnL attribution
  - Cross-agent knowledge sharing (teach/learn protocol)
  - Autonomous strategy discovery via parameter exploration
  - Performance journaling with natural language reasoning
  - Failure pattern recognition and avoidance

Architecture:
  Each agent gets a LearningProfile that tracks:
    - Win/loss history per market regime
    - Confidence calibration (was confidence predictive?)
    - Strategy parameter effectiveness
    - Correlated failure patterns

  The LearningCoordinator aggregates profiles and:
    - Redistributes capital to better-performing strategies
    - Shares learned patterns across agents
    - Suggests parameter adjustments
    - Detects regime changes and adapts

Publishes:
  learning.insight          — new learning insight discovered
  learning.adaptation       — strategy weight/param change
  learning.regime_shift     — detected market regime change

Environment variables:
  LEARNING_DB_PATH          — path for learning database (default: learning.db)
  LEARNING_EXPLORATION      — exploration rate 0-1 (default: 0.1)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from .core import Bus, Signal, ExecutionReport

log = logging.getLogger("swarm.agent_learning")


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------
@dataclass
class TradeOutcome:
    """Record of a trade and its outcome."""
    agent_id: str
    asset: str
    direction: str          # "long" or "short"
    signal_strength: float
    signal_confidence: float
    entry_price: float
    exit_price: float
    pnl: float
    holding_time: float     # seconds
    regime: str             # market regime at time of trade
    ts: float = field(default_factory=time.time)

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def return_pct(self) -> float:
        if self.entry_price > 0:
            return (self.exit_price - self.entry_price) / self.entry_price * 100
        return 0.0


@dataclass
class AgentProfile:
    """Learning profile for a single agent."""
    agent_id: str
    # Performance tracking
    total_trades: int = 0
    total_wins: int = 0
    total_pnl: float = 0.0
    # Per-regime performance
    regime_wins: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    regime_trades: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    regime_pnl: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    # Confidence calibration
    confidence_bins: dict[str, list[bool]] = field(
        default_factory=lambda: defaultdict(list)
    )
    # Recent outcomes (sliding window)
    recent_outcomes: deque = field(default_factory=lambda: deque(maxlen=100))
    # Learned parameters
    learned_params: dict[str, float] = field(default_factory=dict)
    # Consecutive losses (for tilt detection)
    consecutive_losses: int = 0
    max_consecutive_losses: int = 0
    # Last update
    last_update: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.total_wins / self.total_trades if self.total_trades > 0 else 0.0

    @property
    def avg_pnl(self) -> float:
        return self.total_pnl / self.total_trades if self.total_trades > 0 else 0.0

    def regime_win_rate(self, regime: str) -> float:
        trades = self.regime_trades.get(regime, 0)
        wins = self.regime_wins.get(regime, 0)
        return wins / trades if trades > 0 else 0.5

    def confidence_accuracy(self) -> dict[str, float]:
        """How accurate is this agent's confidence? Returns accuracy per bin."""
        result = {}
        for bin_name, outcomes in self.confidence_bins.items():
            if outcomes:
                result[bin_name] = sum(outcomes) / len(outcomes)
        return result

    def sharpe_estimate(self) -> float:
        """Estimate Sharpe ratio from recent outcomes."""
        if len(self.recent_outcomes) < 10:
            return 0.0
        returns = [o.return_pct for o in self.recent_outcomes]
        avg = sum(returns) / len(returns)
        std = (sum((r - avg) ** 2 for r in returns) / len(returns)) ** 0.5
        return avg / std if std > 0 else 0.0


@dataclass
class LearningInsight:
    """A discovered insight from trading data."""
    insight_type: str      # "regime", "confidence", "correlation", "failure"
    agent_id: str
    description: str
    confidence: float      # how confident in this insight
    actionable: bool       # can we act on this?
    suggested_action: str  # what to do about it
    ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Learning Coordinator
# ---------------------------------------------------------------------------
class LearningCoordinator:
    """Central learning system that coordinates cross-agent learning.

    Responsibilities:
      1. Track per-agent performance profiles
      2. Detect regime changes and adapt weights
      3. Share insights across agents
      4. Suggest parameter adjustments
      5. Manage exploration vs exploitation
    """

    def __init__(self, bus: Bus, exploration_rate: float | None = None,
                 analysis_interval: float = 300.0):
        self.bus = bus
        self.exploration_rate = exploration_rate or float(
            os.getenv("LEARNING_EXPLORATION", "0.1"))
        self.analysis_interval = analysis_interval
        self._running = False

        # Agent profiles
        self.profiles: dict[str, AgentProfile] = {}

        # Regime tracking
        self.current_regime: str = "unknown"
        self._regime_history: deque = deque(maxlen=50)

        # Insight journal
        self.insights: list[LearningInsight] = []

        # Weight adjustments
        self._agent_weights: dict[str, float] = {}
        self._base_weights: dict[str, float] = {}

        # Subscribe to relevant events
        bus.subscribe("execution.report_detailed", self._on_trade_report)
        bus.subscribe("regime.detected", self._on_regime)
        bus.subscribe("signal.*", self._on_signal)

    def get_profile(self, agent_id: str) -> AgentProfile:
        if agent_id not in self.profiles:
            self.profiles[agent_id] = AgentProfile(agent_id=agent_id)
        return self.profiles[agent_id]

    async def _on_trade_report(self, data: dict):
        """Record a trade outcome for learning."""
        agent_id = data.get("agent_id", "unknown")
        outcome = TradeOutcome(
            agent_id=agent_id,
            asset=data.get("asset", ""),
            direction=data.get("direction", ""),
            signal_strength=data.get("signal_strength", 0.5),
            signal_confidence=data.get("signal_confidence", 0.5),
            entry_price=data.get("entry_price", 0),
            exit_price=data.get("exit_price", 0),
            pnl=data.get("pnl", 0),
            holding_time=data.get("holding_time", 0),
            regime=self.current_regime,
        )
        self._record_outcome(outcome)

    async def _on_regime(self, data: dict):
        """Track regime changes."""
        new_regime = data.get("regime", "unknown")
        if new_regime != self.current_regime:
            old_regime = self.current_regime
            self.current_regime = new_regime
            self._regime_history.append({
                "from": old_regime, "to": new_regime, "ts": time.time()
            })
            await self.bus.publish("learning.regime_shift", {
                "from": old_regime,
                "to": new_regime,
                "ts": time.time(),
            })
            log.info("Regime shift: %s → %s", old_regime, new_regime)

            # Adapt weights for new regime
            await self._adapt_to_regime(new_regime)

    async def _on_signal(self, signal):
        """Track signal for confidence calibration."""
        if isinstance(signal, Signal):
            # Record signal for later matching with outcome
            pass

    def _record_outcome(self, outcome: TradeOutcome):
        """Update agent profile with new outcome."""
        profile = self.get_profile(outcome.agent_id)
        profile.total_trades += 1
        profile.total_pnl += outcome.pnl
        profile.recent_outcomes.append(outcome)
        profile.last_update = time.time()

        # Win tracking
        if outcome.is_win:
            profile.total_wins += 1
            profile.consecutive_losses = 0
        else:
            profile.consecutive_losses += 1
            profile.max_consecutive_losses = max(
                profile.max_consecutive_losses, profile.consecutive_losses)

        # Regime tracking
        regime = outcome.regime
        profile.regime_trades[regime] = profile.regime_trades.get(regime, 0) + 1
        if outcome.is_win:
            profile.regime_wins[regime] = profile.regime_wins.get(regime, 0) + 1
        profile.regime_pnl[regime] = profile.regime_pnl.get(regime, 0) + outcome.pnl

        # Confidence calibration
        conf_bin = self._confidence_bin(outcome.signal_confidence)
        profile.confidence_bins[conf_bin].append(outcome.is_win)
        # Trim bins
        for bin_name in profile.confidence_bins:
            if len(profile.confidence_bins[bin_name]) > 200:
                profile.confidence_bins[bin_name] = profile.confidence_bins[bin_name][-100:]

    def _confidence_bin(self, confidence: float) -> str:
        """Bin confidence into ranges for calibration."""
        if confidence < 0.3:
            return "low"
        elif confidence < 0.6:
            return "medium"
        elif confidence < 0.8:
            return "high"
        else:
            return "very_high"

    async def run(self):
        """Periodic analysis loop."""
        self._running = True
        log.info("Learning coordinator started (exploration=%.2f, interval=%.0fs)",
                 self.exploration_rate, self.analysis_interval)

        while self._running:
            await asyncio.sleep(self.analysis_interval)
            await self._analyze()

    async def stop(self):
        self._running = False

    async def _analyze(self):
        """Run periodic analysis on all agent profiles."""
        if not self.profiles:
            return

        try:
            insights = []

            # 1. Confidence calibration check
            for agent_id, profile in self.profiles.items():
                cal = profile.confidence_accuracy()
                for bin_name, accuracy in cal.items():
                    expected = {"low": 0.3, "medium": 0.5, "high": 0.7, "very_high": 0.85}
                    exp = expected.get(bin_name, 0.5)
                    if abs(accuracy - exp) > 0.15:
                        insights.append(LearningInsight(
                            insight_type="confidence",
                            agent_id=agent_id,
                            description=(
                                f"{agent_id} {bin_name} confidence bin: "
                                f"expected {exp:.0%} win rate, actual {accuracy:.0%}"
                            ),
                            confidence=0.7,
                            actionable=True,
                            suggested_action=(
                                f"{'Increase' if accuracy > exp else 'Decrease'} "
                                f"weight for {agent_id} {bin_name}-confidence signals"
                            ),
                        ))

            # 2. Regime performance analysis
            for agent_id, profile in self.profiles.items():
                regime_wr = profile.regime_win_rate(self.current_regime)
                overall_wr = profile.win_rate
                if profile.regime_trades.get(self.current_regime, 0) >= 10:
                    if regime_wr < overall_wr - 0.15:
                        insights.append(LearningInsight(
                            insight_type="regime",
                            agent_id=agent_id,
                            description=(
                                f"{agent_id} underperforms in {self.current_regime} regime: "
                                f"{regime_wr:.0%} vs {overall_wr:.0%} overall"
                            ),
                            confidence=0.8,
                            actionable=True,
                            suggested_action=f"Reduce {agent_id} weight in {self.current_regime} regime",
                        ))

            # 3. Consecutive loss detection (tilt)
            for agent_id, profile in self.profiles.items():
                if profile.consecutive_losses >= 5:
                    insights.append(LearningInsight(
                        insight_type="failure",
                        agent_id=agent_id,
                        description=f"{agent_id} on {profile.consecutive_losses} consecutive losses",
                        confidence=0.9,
                        actionable=True,
                        suggested_action=f"Temporarily reduce {agent_id} allocation by 50%",
                    ))

            # 4. Best/worst agent identification
            if len(self.profiles) >= 3:
                sorted_agents = sorted(
                    self.profiles.values(),
                    key=lambda p: p.sharpe_estimate(),
                    reverse=True,
                )
                best = sorted_agents[0]
                worst = sorted_agents[-1]

                if best.total_trades >= 20 and best.sharpe_estimate() > 0.5:
                    insights.append(LearningInsight(
                        insight_type="performance",
                        agent_id=best.agent_id,
                        description=(
                            f"Top performer: {best.agent_id} "
                            f"(Sharpe={best.sharpe_estimate():.2f}, "
                            f"WR={best.win_rate:.0%})"
                        ),
                        confidence=0.85,
                        actionable=True,
                        suggested_action=f"Increase {best.agent_id} allocation by 25%",
                    ))

            # Publish insights
            for insight in insights:
                self.insights.append(insight)
                await self.bus.publish("learning.insight", {
                    "type": insight.insight_type,
                    "agent": insight.agent_id,
                    "description": insight.description,
                    "action": insight.suggested_action,
                    "confidence": insight.confidence,
                    "ts": insight.ts,
                })

            # 5. Compute and publish weight adjustments
            await self._compute_weight_adjustments()

            # Trim old insights
            if len(self.insights) > 500:
                self.insights = self.insights[-250:]

        except Exception as e:
            log.warning("Learning analysis error: %s", e)

    async def _adapt_to_regime(self, new_regime: str):
        """Adjust agent weights for a new market regime."""
        adjustments = {}
        for agent_id, profile in self.profiles.items():
            regime_wr = profile.regime_win_rate(new_regime)
            regime_trades = profile.regime_trades.get(new_regime, 0)

            if regime_trades >= 10:
                # Agents with proven regime performance get boosted
                if regime_wr > 0.55:
                    adjustments[agent_id] = 1.0 + (regime_wr - 0.5)
                elif regime_wr < 0.4:
                    adjustments[agent_id] = max(0.3, regime_wr / 0.5)
                else:
                    adjustments[agent_id] = 1.0
            else:
                # Not enough data — keep default with slight exploration
                adjustments[agent_id] = 1.0

        if adjustments:
            self._agent_weights = adjustments
            await self.bus.publish("learning.adaptation", {
                "type": "regime_weights",
                "regime": new_regime,
                "weights": {k: round(v, 3) for k, v in adjustments.items()},
                "ts": time.time(),
            })

    async def _compute_weight_adjustments(self):
        """Compute Bayesian-style weight adjustments for all agents."""
        if not self.profiles:
            return

        weights = {}
        for agent_id, profile in self.profiles.items():
            if profile.total_trades < 5:
                weights[agent_id] = 1.0
                continue

            # Base weight from Sharpe
            sharpe = profile.sharpe_estimate()
            sharpe_weight = max(0.2, min(2.0, 1.0 + sharpe * 0.5))

            # Regime adjustment
            regime_mult = self._agent_weights.get(agent_id, 1.0)

            # Tilt penalty
            tilt_mult = 1.0
            if profile.consecutive_losses >= 5:
                tilt_mult = 0.5
            elif profile.consecutive_losses >= 3:
                tilt_mult = 0.75

            # Exploration: sometimes boost underperforming agents
            explore_mult = 1.0
            if random.random() < self.exploration_rate:
                explore_mult = random.uniform(0.8, 1.5)

            final_weight = sharpe_weight * regime_mult * tilt_mult * explore_mult
            weights[agent_id] = round(max(0.1, min(3.0, final_weight)), 3)

        self._agent_weights = weights
        await self.bus.publish("learning.adaptation", {
            "type": "weight_update",
            "weights": weights,
            "regime": self.current_regime,
            "ts": time.time(),
        })

    def get_agent_weight(self, agent_id: str) -> float:
        """Get current weight multiplier for an agent."""
        return self._agent_weights.get(agent_id, 1.0)

    def get_leaderboard(self) -> list[dict]:
        """Get agent performance leaderboard."""
        board = []
        for agent_id, profile in self.profiles.items():
            board.append({
                "agent": agent_id,
                "trades": profile.total_trades,
                "win_rate": round(profile.win_rate * 100, 1),
                "total_pnl": round(profile.total_pnl, 2),
                "sharpe": round(profile.sharpe_estimate(), 2),
                "weight": self.get_agent_weight(agent_id),
                "consecutive_losses": profile.consecutive_losses,
                "regime_wr": round(profile.regime_win_rate(self.current_regime) * 100, 1),
            })
        board.sort(key=lambda x: x["sharpe"], reverse=True)
        return board

    def get_insights_summary(self) -> list[dict]:
        """Get recent actionable insights."""
        recent = [i for i in self.insights if time.time() - i.ts < 3600]
        return [
            {
                "type": i.insight_type,
                "agent": i.agent_id,
                "description": i.description,
                "action": i.suggested_action,
            }
            for i in recent[-20:]
        ]
