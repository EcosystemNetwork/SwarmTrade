"""Capital allocation leaderboard — top-performing strategies get more capital.

Tracks performance of internal agents and external gateway agents,
ranks them by risk-adjusted returns, and allocates more capital
to consistently profitable signal sources.

Inspired by CipherTradeArena's capital allocation model (50/30/20 split).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .core import Bus, Signal, ExecutionReport

log = logging.getLogger("swarm.capital")


@dataclass
class AgentPerformance:
    """Tracks an agent's signal accuracy and contribution."""
    agent_id: str
    signals_sent: int = 0
    signals_correct: int = 0  # signal direction matched actual PnL
    total_contribution: float = 0.0  # sum of (strength * confidence * realized_pnl_sign)
    total_pnl_attributed: float = 0.0  # PnL attributed to this agent's signals
    avg_confidence: float = 0.0
    last_signal_ts: float = 0.0

    @property
    def accuracy(self) -> float:
        return self.signals_correct / max(1, self.signals_sent)

    @property
    def score(self) -> float:
        """Composite score: accuracy * average confidence * signal volume."""
        return self.accuracy * self.avg_confidence * min(self.signals_sent, 50) / 50


class CapitalAllocator:
    """Tracks agent performance and computes capital allocation shares.

    Allocation model (inspired by CipherTradeArena):
    - Top performer: 40% of allocated capital
    - Second: 25%
    - Third: 15%
    - Remaining: split equally among rest (20%)

    Also serves as a leaderboard for the dashboard.
    """

    def __init__(self, bus: Bus, total_capital: float = 10_000.0):
        self.bus = bus
        self.total_capital = total_capital
        self._agents: dict[str, AgentPerformance] = {}
        self._recent_signals: dict[str, Signal] = {}  # intent_id -> signal that contributed

        bus.subscribe("signal.*", self._on_signal)
        bus.subscribe("exec.report", self._on_report)
        bus.subscribe("audit.attribution", self._on_attribution)

    async def _on_signal(self, sig: Signal):
        """Track every signal."""
        if sig.agent_id not in self._agents:
            self._agents[sig.agent_id] = AgentPerformance(agent_id=sig.agent_id)

        perf = self._agents[sig.agent_id]
        perf.signals_sent += 1
        perf.last_signal_ts = sig.ts
        # Running average of confidence
        perf.avg_confidence = (
            (perf.avg_confidence * (perf.signals_sent - 1) + sig.confidence)
            / perf.signals_sent
        )

    async def _on_report(self, report: ExecutionReport):
        """Track fill outcomes."""
        if report.status != "filled":
            return
        # Note the fill for attribution
        pass

    async def _on_attribution(self, payload: dict):
        """Track PnL attribution to agents."""
        pnl = payload.get("pnl", 0.0)
        contribs = payload.get("contribs", {})

        for agent_id, contribution in contribs.items():
            if agent_id not in self._agents:
                self._agents[agent_id] = AgentPerformance(agent_id=agent_id)

            perf = self._agents[agent_id]
            perf.total_contribution += contribution
            perf.total_pnl_attributed += pnl * abs(contribution)

            # Was the signal direction correct?
            if (contribution > 0 and pnl > 0) or (contribution < 0 and pnl < 0):
                perf.signals_correct += 1

    def leaderboard(self) -> list[dict]:
        """Get ranked agent performance leaderboard."""
        ranked = sorted(
            self._agents.values(),
            key=lambda p: p.score,
            reverse=True,
        )
        return [
            {
                "rank": i + 1,
                "agent_id": p.agent_id,
                "signals": p.signals_sent,
                "accuracy": round(p.accuracy * 100, 1),
                "avg_confidence": round(p.avg_confidence, 3),
                "score": round(p.score, 4),
                "pnl_attributed": round(p.total_pnl_attributed, 2),
                "allocation_pct": round(alloc * 100, 1),
            }
            for i, (p, alloc) in enumerate(zip(ranked, self._compute_allocations(ranked)))
        ]

    def _compute_allocations(self, ranked: list[AgentPerformance]) -> list[float]:
        """Compute capital allocation percentages (40/25/15/20 split)."""
        n = len(ranked)
        if n == 0:
            return []
        if n == 1:
            return [1.0]
        if n == 2:
            return [0.6, 0.4]
        if n == 3:
            return [0.40, 0.25, 0.35]

        allocations = [0.0] * n
        allocations[0] = 0.40
        allocations[1] = 0.25
        allocations[2] = 0.15
        # Split remaining 20% equally
        remaining = n - 3
        if remaining > 0:
            each = 0.20 / remaining
            for i in range(3, n):
                allocations[i] = each
        return allocations

    def get_allocation(self, agent_id: str) -> float:
        """Get the capital allocation for a specific agent."""
        lb = self.leaderboard()
        for entry in lb:
            if entry["agent_id"] == agent_id:
                return entry["allocation_pct"] / 100.0
        return 0.0

    def summary(self) -> dict:
        """Full allocator state for dashboard."""
        lb = self.leaderboard()
        return {
            "total_capital": self.total_capital,
            "agents_tracked": len(self._agents),
            "leaderboard": lb[:20],  # top 20
            "top_performer": lb[0] if lb else None,
        }
