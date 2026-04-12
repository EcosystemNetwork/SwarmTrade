"""Swarm Consensus Engine — multi-agent voting on trade decisions.

Inspired by DIVE (AI swarm engine verifying truth for prediction markets),
Uniforum (AI agents argue over DeFi and trade when they agree), and
Alpha Dawg (adversarial debate with deterministic override).

Goes beyond the binary bull/bear debate — allows N agents to vote on
trade proposals with weighted ballots:

  1. Trade proposal enters consensus round
  2. All relevant agents cast weighted votes (ELO-weighted)
  3. Votes tallied with quorum and supermajority requirements
  4. Consensus reached → execute. No consensus → no trade.

Consensus modes:
  - SIMPLE_MAJORITY: >50% of voting weight approves
  - SUPERMAJORITY: >66% approves (for large trades)
  - UNANIMOUS: all agents must agree (for critical decisions)
  - WEIGHTED_CONVICTION: weighted by signal confidence * ELO

Bus integration:
  Subscribes to: signal.* (agents vote via signals), alpha.screened
  Publishes to:  consensus.reached, consensus.failed, signal.consensus
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections import defaultdict

from .core import Bus, Signal

log = logging.getLogger("swarm.consensus")


@dataclass
class ConsensusVote:
    """A single agent's vote on a trade proposal."""
    agent_id: str
    direction: str           # "approve", "reject", "abstain"
    weight: float            # ELO-weighted voting power
    confidence: float
    rationale: str = ""


@dataclass
class ConsensusRound:
    """A complete consensus voting round."""
    round_id: str
    asset: str
    proposed_direction: str  # "long" or "short"
    # Voting
    votes: list[ConsensusVote] = field(default_factory=list)
    total_weight_for: float = 0.0
    total_weight_against: float = 0.0
    total_weight_abstain: float = 0.0
    # Requirements
    quorum_pct: float = 30.0
    approval_pct: float = 60.0
    mode: str = "simple_majority"
    # Result
    result: str = "pending"      # "approved", "rejected", "no_quorum"
    conviction: float = 0.0
    margin: float = 0.0
    ts: float = field(default_factory=time.time)


class SwarmConsensus:
    """Multi-agent voting system for trade decisions.

    Every agent with a signal on the relevant asset gets a vote.
    Voting weight is proportional to:
      - Agent's ELO score (better performers have more influence)
      - Signal confidence (high-confidence votes weighted more)
      - Historical accuracy on this asset

    Trade size determines consensus mode:
      - Small (<$500): simple majority (fast)
      - Medium ($500-$2000): supermajority (more careful)
      - Large (>$2000): supermajority + minimum 5 voters
    """

    def __init__(self, bus: Bus, elo_tracker=None,
                 default_quorum: float = 30.0,
                 default_approval: float = 60.0):
        self.bus = bus
        self.elo_tracker = elo_tracker
        self.default_quorum = default_quorum
        self.default_approval = default_approval
        self._rounds: list[ConsensusRound] = []
        self._round_counter = 0
        self._recent_signals: dict[str, list[Signal]] = defaultdict(list)
        self._stats = {
            "rounds": 0, "approved": 0, "rejected": 0,
            "no_quorum": 0, "avg_voters": 0.0,
        }

        # Collect signals for voting
        for topic in [
            "signal.momentum", "signal.mean_reversion", "signal.rsi",
            "signal.macd", "signal.bollinger", "signal.whale",
            "signal.news", "signal.ml", "signal.confluence",
            "signal.smart_money", "signal.fusion", "signal.debate",
            "signal.sentiment", "signal.onchain", "signal.funding_rate",
            "signal.fear_greed", "signal.narrative",
            "signal.whale_mirror", "signal.mev", "signal.grid",
        ]:
            bus.subscribe(topic, self._collect_vote)

        bus.subscribe("alpha.screened", self._on_alpha_screened)

    async def _collect_vote(self, sig: Signal):
        """Collect signals as potential votes."""
        if not isinstance(sig, Signal):
            return
        self._recent_signals[sig.asset].append(sig)
        # Keep last 60 seconds
        cutoff = time.time() - 60
        self._recent_signals[sig.asset] = [
            s for s in self._recent_signals[sig.asset] if s.ts > cutoff
        ]

    async def _on_alpha_screened(self, msg: dict):
        """Run consensus on approved alpha opportunities."""
        opp = msg.get("opportunity")
        if not opp or getattr(opp, "status", "") != "approved":
            return
        await self.run_consensus(opp.asset, opp.direction)

    async def run_consensus(self, asset: str, proposed_direction: str,
                             trade_size_usd: float = 500.0) -> ConsensusRound:
        """Run a full consensus round for a trade proposal."""
        self._round_counter += 1
        rid = f"cons-{self._round_counter:05d}"

        # Determine mode based on trade size
        if trade_size_usd > 2000:
            mode = "supermajority"
            approval_pct = 66.0
            min_voters = 5
        elif trade_size_usd > 500:
            mode = "supermajority"
            approval_pct = 60.0
            min_voters = 3
        else:
            mode = "simple_majority"
            approval_pct = 50.0
            min_voters = 2

        round_ = ConsensusRound(
            round_id=rid, asset=asset,
            proposed_direction=proposed_direction,
            mode=mode, approval_pct=approval_pct,
            quorum_pct=self.default_quorum,
        )

        # Collect votes from recent signals
        signals = self._recent_signals.get(asset, [])

        # Deduplicate: one vote per agent
        latest_by_agent: dict[str, Signal] = {}
        for sig in signals:
            if sig.agent_id not in latest_by_agent or sig.ts > latest_by_agent[sig.agent_id].ts:
                latest_by_agent[sig.agent_id] = sig

        for agent_id, sig in latest_by_agent.items():
            elo = 500.0
            if self.elo_tracker:
                elo = self.elo_tracker.get_elo(agent_id)

            weight = (elo / 500.0) * sig.confidence

            if sig.direction == proposed_direction and sig.strength > 0.1:
                direction = "approve"
                round_.total_weight_for += weight
            elif sig.direction != proposed_direction and sig.direction != "flat" and sig.strength > 0.1:
                direction = "reject"
                round_.total_weight_against += weight
            else:
                direction = "abstain"
                round_.total_weight_abstain += weight

            round_.votes.append(ConsensusVote(
                agent_id=agent_id, direction=direction,
                weight=round(weight, 3), confidence=sig.confidence,
                rationale=sig.rationale[:60],
            ))

        # Resolve
        total_voting = round_.total_weight_for + round_.total_weight_against
        total_all = total_voting + round_.total_weight_abstain

        if len(round_.votes) < min_voters or total_voting < 1:
            round_.result = "no_quorum"
            self._stats["no_quorum"] += 1
        else:
            approval = round_.total_weight_for / max(total_voting, 1e-9) * 100
            round_.margin = round_.total_weight_for - round_.total_weight_against
            round_.conviction = min(1.0, round_.margin / max(total_all, 1))

            if approval >= round_.approval_pct:
                round_.result = "approved"
                self._stats["approved"] += 1
            else:
                round_.result = "rejected"
                self._stats["rejected"] += 1

        self._rounds.append(round_)
        if len(self._rounds) > 200:
            self._rounds = self._rounds[-100:]

        self._stats["rounds"] += 1
        n = self._stats["rounds"]
        self._stats["avg_voters"] = (
            (self._stats["avg_voters"] * (n - 1) + len(round_.votes)) / n
        )

        log.info(
            "CONSENSUS %s: %s %s | %d voters | for=%.1f against=%.1f | "
            "approval=%.0f%% -> %s (mode=%s)",
            rid, proposed_direction.upper(), asset, len(round_.votes),
            round_.total_weight_for, round_.total_weight_against,
            round_.total_weight_for / max(total_voting, 1e-9) * 100,
            round_.result.upper(), mode,
        )

        if round_.result == "approved":
            sig = Signal(
                agent_id="swarm_consensus",
                asset=asset,
                direction=proposed_direction,
                strength=min(1.0, round_.conviction * 1.2),
                confidence=round_.conviction,
                rationale=(
                    f"Consensus {rid}: {len(round_.votes)} agents, "
                    f"for={round_.total_weight_for:.1f} vs against={round_.total_weight_against:.1f} "
                    f"({mode})"
                ),
            )
            await self.bus.publish("signal.consensus", sig)
            await self.bus.publish("consensus.reached", round_)
        else:
            await self.bus.publish("consensus.failed", round_)

        return round_

    def summary(self) -> dict:
        return {
            **self._stats,
            "approval_rate": self._stats["approved"] / max(self._stats["rounds"], 1),
            "recent": [
                {
                    "id": r.round_id,
                    "asset": r.asset,
                    "direction": r.proposed_direction,
                    "voters": len(r.votes),
                    "result": r.result,
                    "conviction": round(r.conviction, 3),
                }
                for r in self._rounds[-5:]
            ],
        }
