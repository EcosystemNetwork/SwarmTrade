"""Agent Memory System v2 — long-term learning with memory DAG.

Inspired by Alpha Dawg (memory DAG with priorCids, not flat log),
Colony (database-backed state for agent coordination), and the broader
pattern of agents that learn from historical outcomes.

Upgrades the existing memory.py with:
  1. Memory DAG: each entry references prior entries (not flat list)
  2. Semantic retrieval: find memories by similarity, not just recency
  3. Decay: old memories lose weight unless reinforced
  4. Consolidation: merge similar memories into compressed summaries
  5. Per-agent memory: each agent has its own memory lane + shared pool

Bus integration:
  Subscribes to: exec.report, debate.resolved, evolution.champion
  Publishes to:  memory.stored, memory.recalled
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from collections import defaultdict

from .core import Bus, ExecutionReport

log = logging.getLogger("swarm.memory_v2")


@dataclass
class MemoryEntry:
    """A single memory in the DAG."""
    memory_id: str
    agent_id: str          # which agent created this memory
    # Content
    category: str          # "trade_outcome", "debate", "pattern", "lesson", "strategy"
    content: str
    metadata: dict = field(default_factory=dict)
    # DAG links
    prior_ids: list[str] = field(default_factory=list)  # memories this references
    referenced_by: list[str] = field(default_factory=list)  # memories that reference this
    # Relevance
    importance: float = 0.5     # 0=trivial, 1=critical
    decay_rate: float = 0.01   # importance decays by this per day
    reinforcement_count: int = 0  # times this memory was recalled/reinforced
    # Embedding (for semantic search — simplified as keyword list)
    keywords: list[str] = field(default_factory=list)
    # Lifecycle
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    consolidated_into: str | None = None  # if merged into another memory


class AgentMemoryDAG:
    """DAG-structured memory system for the swarm.

    Each memory entry can reference prior entries, creating a directed
    acyclic graph of knowledge. This allows agents to trace reasoning
    chains and learn from sequences of outcomes.

    Example chain:
      memory-001: "ETH RSI oversold at 25" (pattern)
        -> memory-002: "Bought ETH based on RSI oversold" (trade)
          -> memory-003: "ETH trade +2.5% — RSI oversold worked" (outcome)
            -> memory-004: "RSI oversold reliable when funding negative" (lesson)
    """

    def __init__(self, bus: Bus, max_memories_per_agent: int = 500,
                 consolidation_threshold: int = 100):
        self.bus = bus
        self.max_per_agent = max_memories_per_agent
        self.consolidation_threshold = consolidation_threshold
        self._memories: dict[str, MemoryEntry] = {}
        self._agent_memories: dict[str, list[str]] = defaultdict(list)
        self._shared_pool: list[str] = []  # memory IDs accessible to all
        self._counter = 0
        self._stats = {"stored": 0, "recalled": 0, "consolidated": 0, "decayed": 0}

        bus.subscribe("exec.report", self._on_execution)
        bus.subscribe("debate.resolved", self._on_debate)

    def store(self, agent_id: str, category: str, content: str,
              importance: float = 0.5, prior_ids: list[str] | None = None,
              keywords: list[str] | None = None,
              metadata: dict | None = None, shared: bool = False) -> MemoryEntry:
        """Store a new memory in the DAG."""
        self._counter += 1
        mid = f"mem-{self._counter:06d}"

        entry = MemoryEntry(
            memory_id=mid,
            agent_id=agent_id,
            category=category,
            content=content,
            metadata=metadata or {},
            prior_ids=prior_ids or [],
            importance=importance,
            keywords=keywords or self._extract_keywords(content),
        )

        # Link DAG references
        for pid in entry.prior_ids:
            prior = self._memories.get(pid)
            if prior:
                prior.referenced_by.append(mid)

        self._memories[mid] = entry
        self._agent_memories[agent_id].append(mid)
        if shared:
            self._shared_pool.append(mid)

        self._stats["stored"] += 1

        # Trim if agent has too many memories
        if len(self._agent_memories[agent_id]) > self.max_per_agent:
            self._evict_least_important(agent_id)

        return entry

    def recall(self, agent_id: str, query: str = "", category: str = "",
               limit: int = 5, include_shared: bool = True) -> list[MemoryEntry]:
        """Recall memories relevant to a query.

        Retrieval priority:
          1. Keyword match (most relevant)
          2. Same category
          3. Recent + high importance
          4. Shared pool (if included)
        """
        candidates = []

        # Agent's own memories
        for mid in self._agent_memories.get(agent_id, []):
            mem = self._memories.get(mid)
            if mem and not mem.consolidated_into:
                candidates.append(mem)

        # Shared pool
        if include_shared:
            for mid in self._shared_pool:
                mem = self._memories.get(mid)
                if mem and mem.agent_id != agent_id and not mem.consolidated_into:
                    candidates.append(mem)

        # Filter by category
        if category:
            candidates = [m for m in candidates if m.category == category]

        # Score by relevance
        query_words = set(query.lower().split()) if query else set()
        scored = []
        for mem in candidates:
            score = mem.importance * (1 + mem.reinforcement_count * 0.1)

            # Keyword match bonus
            if query_words:
                keyword_set = set(k.lower() for k in mem.keywords)
                overlap = len(query_words & keyword_set)
                score += overlap * 0.3

            # Recency bonus (exponential decay)
            age_days = (time.time() - mem.created_at) / 86400
            recency = max(0.1, 1.0 - age_days * mem.decay_rate)
            score *= recency

            scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)
        results = [mem for _, mem in scored[:limit]]

        # Mark as accessed (reinforcement)
        for mem in results:
            mem.last_accessed = time.time()
            mem.reinforcement_count += 1

        self._stats["recalled"] += len(results)
        return results

    def recall_chain(self, memory_id: str, depth: int = 3) -> list[MemoryEntry]:
        """Follow the DAG backward from a memory to find its reasoning chain."""
        chain = []
        visited = set()
        queue = [memory_id]

        while queue and len(chain) < depth * 3:
            current_id = queue.pop(0)
            if current_id in visited:
                continue
            visited.add(current_id)

            mem = self._memories.get(current_id)
            if mem:
                chain.append(mem)
                queue.extend(mem.prior_ids)

        return chain

    async def _on_execution(self, report: ExecutionReport):
        """Store trade outcomes as memories."""
        if report.status != "filled":
            return
        pnl = report.pnl_estimate or 0
        outcome = "profitable" if pnl > 0 else "losing" if pnl < 0 else "breakeven"

        self.store(
            agent_id="execution",
            category="trade_outcome",
            content=f"{report.side} {report.quantity:.4f} {report.asset} @ ${report.fill_price:.2f} -> {outcome} (${pnl:+.4f})",
            importance=min(1.0, abs(pnl) / 10 + 0.3),
            keywords=[report.asset, report.side, outcome, "trade"],
            metadata={"pnl": pnl, "asset": report.asset, "side": report.side},
            shared=abs(pnl) > 1.0,  # significant trades shared with all agents
        )

    async def _on_debate(self, data: dict):
        """Store debate outcomes as memories."""
        record = data.get("record")
        if not record:
            return
        verdict = data.get("verdict", "")

        # Find recent trade memories for this asset to link
        recent = self.recall("debate_engine", query=record.asset,
                           category="trade_outcome", limit=2)
        prior_ids = [m.memory_id for m in recent]

        self.store(
            agent_id="debate_engine",
            category="debate",
            content=f"Debate {record.debate_id}: {record.direction} {record.asset} -> {verdict} (margin={record.margin:.2f})",
            importance=0.6 if verdict == "approve" else 0.4,
            prior_ids=prior_ids,
            keywords=[record.asset, record.direction, verdict, "debate"],
            metadata={"margin": record.margin, "conviction": record.conviction},
            shared=True,
        )

    def consolidate(self, agent_id: str):
        """Merge similar old memories into compressed summaries."""
        memories = [
            self._memories[mid]
            for mid in self._agent_memories.get(agent_id, [])
            if mid in self._memories and not self._memories[mid].consolidated_into
        ]

        if len(memories) < self.consolidation_threshold:
            return 0

        # Group by category + asset
        groups: dict[str, list[MemoryEntry]] = defaultdict(list)
        for mem in memories:
            asset = mem.metadata.get("asset", "general")
            key = f"{mem.category}:{asset}"
            groups[key].append(mem)

        consolidated = 0
        for key, group in groups.items():
            if len(group) < 5:
                continue

            # Keep most important, consolidate rest
            group.sort(key=lambda m: m.importance, reverse=True)
            keepers = group[:3]
            to_merge = group[3:]

            # Create summary memory
            summary_content = f"Consolidated {len(to_merge)} {key} memories: "
            outcomes = [m.metadata.get("pnl", 0) for m in to_merge if "pnl" in m.metadata]
            if outcomes:
                summary_content += f"avg PnL=${sum(outcomes)/len(outcomes):+.4f}, n={len(outcomes)}"

            summary = self.store(
                agent_id=agent_id,
                category="consolidated",
                content=summary_content,
                importance=max(m.importance for m in to_merge) * 0.8,
                prior_ids=[k.memory_id for k in keepers],
                keywords=list(set(kw for m in to_merge for kw in m.keywords)),
            )

            for mem in to_merge:
                mem.consolidated_into = summary.memory_id

            consolidated += len(to_merge)

        self._stats["consolidated"] += consolidated
        return consolidated

    def _evict_least_important(self, agent_id: str):
        """Remove least important memories when limit reached."""
        mids = self._agent_memories.get(agent_id, [])
        if len(mids) <= self.max_per_agent:
            return

        scored = []
        for mid in mids:
            mem = self._memories.get(mid)
            if mem and not mem.consolidated_into:
                age_days = (time.time() - mem.created_at) / 86400
                score = mem.importance * (1 + mem.reinforcement_count * 0.1) * max(0.01, 1 - age_days * mem.decay_rate)
                scored.append((score, mid))

        scored.sort(key=lambda x: x[0])
        to_remove = scored[:len(mids) - self.max_per_agent]
        for _, mid in to_remove:
            self._memories.pop(mid, None)
            self._agent_memories[agent_id].remove(mid)
            self._stats["decayed"] += 1

    def _extract_keywords(self, text: str) -> list[str]:
        """Extract simple keywords from text."""
        stop_words = {"the", "a", "an", "is", "was", "at", "to", "of", "in", "for", "on", "by", "->"}
        words = text.lower().replace("$", "").replace("+", "").replace("-", "").split()
        return [w for w in words if len(w) > 2 and w not in stop_words][:10]

    def summary(self) -> dict:
        active = [m for m in self._memories.values() if not m.consolidated_into]
        return {
            **self._stats,
            "total_memories": len(active),
            "agents_with_memory": len(self._agent_memories),
            "shared_pool_size": len(self._shared_pool),
            "categories": dict(defaultdict(int, {
                m.category: sum(1 for x in active if x.category == m.category)
                for m in active
            })) if active else {},
        }
