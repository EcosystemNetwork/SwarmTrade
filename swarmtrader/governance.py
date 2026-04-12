"""Agent Governance DAO — decentralized decision-making for the swarm.

Inspired by Synapse Treasury (agentic DAO treasury manager), DAO Wizard
(AI-driven DAO creation), and the broader trend of agent-governed
autonomous organizations.

Enables decentralized governance over:
  1. Strategy activation/deactivation (which agents run)
  2. Risk parameter changes (max leverage, daily limits)
  3. Capital allocation (how much to each strategy/chain)
  4. Fee structure changes (marketplace fees, royalties)
  5. Agent admission/removal (who can join the swarm)

Voting is weighted by agent ELO score — better-performing agents
have more governance power. This creates meritocratic decision-making.

Bus integration:
  Subscribes to: governance.proposal, governance.vote
  Publishes to:  governance.passed, governance.rejected, governance.executed

No external dependencies.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .core import Bus

log = logging.getLogger("swarm.governance")


@dataclass
class Proposal:
    """A governance proposal for the swarm."""
    proposal_id: str
    proposer: str           # agent_id or wallet address
    title: str
    description: str
    # Proposal type
    category: str           # "strategy", "risk", "allocation", "fee", "agent"
    # Parameters to change
    changes: dict = field(default_factory=dict)
    # Voting
    votes_for: float = 0.0       # ELO-weighted votes
    votes_against: float = 0.0
    voters: dict = field(default_factory=dict)  # agent_id -> {"vote": bool, "weight": float}
    # Lifecycle
    status: str = "active"       # active, passed, rejected, executed, expired
    quorum_pct: float = 30.0     # minimum participation to be valid
    approval_pct: float = 60.0   # votes_for % needed to pass
    voting_period_s: float = 86400.0  # 24 hours
    created_at: float = field(default_factory=time.time)
    resolved_at: float = 0.0
    executed_at: float = 0.0


@dataclass
class GovernanceAction:
    """Record of an executed governance action."""
    action_id: str
    proposal_id: str
    category: str
    changes: dict
    executed_by: str
    ts: float = field(default_factory=time.time)


class GovernanceDAO:
    """Decentralized governance system for the agent swarm.

    Any agent can propose changes. Voting is weighted by ELO score
    (from the debate engine). Proposals need:
      - Quorum: 30% of total ELO must participate
      - Approval: 60% of participating ELO must vote yes

    Emergency proposals (risk-related) have shortened voting periods
    and lower quorum requirements.
    """

    def __init__(self, bus: Bus, elo_tracker=None):
        self.bus = bus
        self.elo_tracker = elo_tracker
        self._proposals: dict[str, Proposal] = {}
        self._actions: list[GovernanceAction] = []
        self._proposal_counter = 0
        self._stats = {
            "proposed": 0, "passed": 0, "rejected": 0,
            "executed": 0, "total_votes": 0,
        }

        bus.subscribe("governance.proposal", self._on_proposal)
        bus.subscribe("governance.vote", self._on_vote)

    def propose(self, proposer: str, title: str, description: str,
                category: str, changes: dict,
                emergency: bool = False) -> Proposal:
        """Create a new governance proposal."""
        self._proposal_counter += 1
        pid = f"prop-{self._proposal_counter:04d}"

        voting_period = 3600.0 if emergency else 86400.0  # 1h emergency, 24h normal
        quorum = 15.0 if emergency else 30.0

        proposal = Proposal(
            proposal_id=pid, proposer=proposer,
            title=title, description=description,
            category=category, changes=changes,
            quorum_pct=quorum, voting_period_s=voting_period,
        )
        self._proposals[pid] = proposal
        self._stats["proposed"] += 1

        log.info(
            "GOVERNANCE PROPOSAL: %s by %s — '%s' (%s, %s)",
            pid, proposer, title, category,
            "EMERGENCY" if emergency else "standard",
        )
        return proposal

    def vote(self, proposal_id: str, voter: str, approve: bool) -> bool:
        """Cast a vote on a proposal."""
        proposal = self._proposals.get(proposal_id)
        if not proposal or proposal.status != "active":
            return False

        # Check voting period
        if time.time() - proposal.created_at > proposal.voting_period_s:
            self._resolve(proposal)
            return False

        # Prevent double voting
        if voter in proposal.voters:
            return False

        # ELO-weighted vote
        weight = 500.0  # default
        if self.elo_tracker:
            weight = self.elo_tracker.get_elo(voter)

        proposal.voters[voter] = {"vote": approve, "weight": weight}
        if approve:
            proposal.votes_for += weight
        else:
            proposal.votes_against += weight

        self._stats["total_votes"] += 1

        log.info(
            "GOVERNANCE VOTE: %s votes %s on %s (weight=%.0f, for=%.0f, against=%.0f)",
            voter, "YES" if approve else "NO", proposal_id,
            weight, proposal.votes_for, proposal.votes_against,
        )

        # Auto-resolve if enough votes
        total_votes = proposal.votes_for + proposal.votes_against
        total_elo = self._total_elo()
        if total_elo > 0 and (total_votes / total_elo * 100) >= proposal.quorum_pct:
            self._resolve(proposal)

        return True

    def _resolve(self, proposal: Proposal):
        """Resolve a proposal after voting."""
        if proposal.status != "active":
            return

        total_votes = proposal.votes_for + proposal.votes_against
        total_elo = self._total_elo()

        # Check quorum
        participation = (total_votes / max(total_elo, 1)) * 100
        if participation < proposal.quorum_pct:
            proposal.status = "expired"
            log.info("GOVERNANCE: %s expired (participation=%.1f%% < %.1f%% quorum)",
                     proposal.proposal_id, participation, proposal.quorum_pct)
            return

        # Check approval
        approval = (proposal.votes_for / max(total_votes, 1)) * 100
        if approval >= proposal.approval_pct:
            proposal.status = "passed"
            proposal.resolved_at = time.time()
            self._stats["passed"] += 1
            log.info(
                "GOVERNANCE PASSED: %s '%s' (%.1f%% approval, %.1f%% participation)",
                proposal.proposal_id, proposal.title, approval, participation,
            )
        else:
            proposal.status = "rejected"
            proposal.resolved_at = time.time()
            self._stats["rejected"] += 1
            log.info(
                "GOVERNANCE REJECTED: %s '%s' (%.1f%% approval < %.1f%% required)",
                proposal.proposal_id, proposal.title, approval, proposal.approval_pct,
            )

    async def execute(self, proposal_id: str, executor: str = "system") -> bool:
        """Execute a passed proposal."""
        proposal = self._proposals.get(proposal_id)
        if not proposal or proposal.status != "passed":
            return False

        action = GovernanceAction(
            action_id=f"gov-{len(self._actions) + 1:04d}",
            proposal_id=proposal_id,
            category=proposal.category,
            changes=proposal.changes,
            executed_by=executor,
        )
        self._actions.append(action)
        proposal.status = "executed"
        proposal.executed_at = time.time()
        self._stats["executed"] += 1

        log.info(
            "GOVERNANCE EXECUTED: %s '%s' — changes: %s",
            proposal_id, proposal.title, proposal.changes,
        )

        await self.bus.publish("governance.executed", {
            "proposal": proposal, "action": action,
        })
        return True

    async def _on_proposal(self, data):
        if isinstance(data, dict):
            self.propose(
                data.get("proposer", "system"),
                data.get("title", ""),
                data.get("description", ""),
                data.get("category", "general"),
                data.get("changes", {}),
                data.get("emergency", False),
            )

    async def _on_vote(self, data):
        if isinstance(data, dict):
            self.vote(
                data.get("proposal_id", ""),
                data.get("voter", ""),
                data.get("approve", False),
            )

    def _total_elo(self) -> float:
        if self.elo_tracker:
            return sum(self.elo_tracker._elos.values()) or 500.0
        return 500.0 * 10  # assume 10 agents at default

    def active_proposals(self) -> list[dict]:
        return [
            {
                "id": p.proposal_id,
                "title": p.title,
                "category": p.category,
                "votes_for": round(p.votes_for, 0),
                "votes_against": round(p.votes_against, 0),
                "voters": len(p.voters),
                "expires_in": max(0, p.voting_period_s - (time.time() - p.created_at)),
            }
            for p in self._proposals.values()
            if p.status == "active"
        ]

    def summary(self) -> dict:
        return {
            **self._stats,
            "active": len([p for p in self._proposals.values() if p.status == "active"]),
            "active_proposals": self.active_proposals(),
            "recent_actions": [
                {
                    "id": a.action_id,
                    "proposal": a.proposal_id,
                    "category": a.category,
                    "changes": a.changes,
                }
                for a in self._actions[-5:]
            ],
        }
