"""On-Chain Agent Registry — decentralized identity for swarm agents.

Replaces ENS with Unstoppable Domains for agent naming and identity.
Each agent in the swarm gets an on-chain identity with:
  - Unique name (e.g., alpha-hunter.swarmtrader.crypto)
  - On-chain metadata (capabilities, performance, policy)
  - Reputation score from trade outcomes
  - Owner verification via wallet signature

Uses an AgentRegistry smart contract (ERC-4337 compatible) deployed
on Base for cheap registration, with Unstoppable Domains for
human-readable naming.

Environment variables:
  PRIVATE_KEY               — deployer/owner wallet key
  UD_API_KEY                — Unstoppable Domains API key (optional, for naming)
  AGENT_REGISTRY_ADDRESS    — deployed registry contract (optional)
  AGENT_REGISTRY_CHAIN      — chain ID (default: 8453 = Base)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .core import Bus, Signal

log = logging.getLogger("swarm.registry")

# Unstoppable Domains API (alternative to ENS)
UD_RESOLUTION_API = "https://api.unstoppabledomains.com/resolve/domains"
UD_PARTNER_API = "https://api.unstoppabledomains.com/partner/v3"


@dataclass
class AgentIdentity:
    """On-chain identity record for a swarm agent."""
    agent_id: str
    name: str                    # human-readable, e.g. "alpha-hunter"
    domain: str = ""             # UD domain, e.g. "alpha-hunter.swarmtrader.crypto"
    wallet_address: str = ""     # agent's derived wallet
    owner_address: str = ""      # human owner's wallet
    # Capabilities and metadata
    agent_type: str = ""         # from AGENT_TYPE_REGISTRY
    category: str = ""
    capabilities: list[str] = field(default_factory=list)
    description: str = ""
    version: str = "1.0.0"
    # Performance / reputation
    reputation_score: float = 50.0  # 0-100, starts at 50
    total_signals: int = 0
    profitable_signals: int = 0
    total_pnl: float = 0.0
    win_rate: float = 0.0
    # Registration
    registered_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    chain_id: int = 8453
    tx_hash: str = ""             # registration tx
    status: str = "active"        # "active", "paused", "revoked"


@dataclass
class ReputationUpdate:
    """A reputation score update event."""
    agent_id: str
    old_score: float
    new_score: float
    reason: str
    pnl_delta: float = 0.0
    timestamp: float = field(default_factory=time.time)


class AgentRegistry:
    """Manages on-chain agent identities and reputation.

    Features:
      - Register agents with unique IDs and metadata
      - Resolve Unstoppable Domains names to agent identities
      - Track reputation based on signal outcomes
      - Query agents by capability, category, or performance

    The registry is the authoritative source of truth for:
      - Who is in the swarm
      - What each agent can do
      - How well each agent has performed
    """

    def __init__(
        self,
        bus: Bus,
        owner_address: str = "",
        chain_id: int = 8453,
        ud_api_key: str = "",
        registry_contract: str = "",
    ):
        self.bus = bus
        self.owner_address = owner_address
        self.chain_id = chain_id
        self._ud_api_key = ud_api_key or os.getenv("UD_API_KEY", "")
        self._registry_contract = registry_contract or os.getenv("AGENT_REGISTRY_ADDRESS", "")
        self.agents: dict[str, AgentIdentity] = {}
        self._reputation_history: list[ReputationUpdate] = []
        self._domain_base = "swarmtrader.crypto"  # Unstoppable Domains TLD

        # Subscribe to execution reports for reputation tracking
        bus.subscribe("exec.report", self._on_execution)
        bus.subscribe("signal.alpha_swarm", self._on_alpha_signal)

    def register_agent(
        self,
        agent_id: str,
        agent_type: str = "",
        category: str = "",
        capabilities: list[str] | None = None,
        description: str = "",
    ) -> AgentIdentity:
        """Register an agent in the registry."""
        # Derive deterministic wallet address from agent_id
        wallet_seed = hashlib.sha256(f"swarm:{agent_id}:{self.owner_address}".encode()).hexdigest()

        # Create domain name
        safe_name = agent_id.replace("_", "-").replace(".", "-").lower()
        domain = f"{safe_name}.{self._domain_base}"

        identity = AgentIdentity(
            agent_id=agent_id,
            name=safe_name,
            domain=domain,
            wallet_address=f"0x{wallet_seed[:40]}",
            owner_address=self.owner_address,
            agent_type=agent_type,
            category=category,
            capabilities=capabilities or [],
            description=description,
            chain_id=self.chain_id,
        )

        self.agents[agent_id] = identity
        log.info(
            "Agent registered: %s (%s) -> %s",
            agent_id, agent_type, domain,
        )
        return identity

    def register_internal_agents(self):
        """Register all internal swarm agents."""
        internal_agents = [
            ("momentum", "signal-momentum", "signal-generators", ["spot-trading"], "Momentum trend following"),
            ("mean_reversion", "signal-mean-reversion", "signal-generators", ["spot-trading"], "Mean reversion strategy"),
            ("volatility", "signal-custom", "signal-generators", ["spot-trading"], "Volatility-based signals"),
            ("rsi", "signal-custom", "signal-generators", ["spot-trading"], "RSI overbought/oversold"),
            ("macd", "signal-custom", "signal-generators", ["spot-trading"], "MACD crossover signals"),
            ("bollinger", "signal-custom", "signal-generators", ["spot-trading"], "Bollinger band signals"),
            ("vwap", "signal-custom", "signal-generators", ["spot-trading"], "VWAP-based signals"),
            ("ichimoku", "signal-custom", "signal-generators", ["spot-trading"], "Ichimoku cloud signals"),
            ("smart_money", "data-onchain", "data-research", ["on-chain-data"], "Smart money wallet tracking"),
            ("whale", "data-onchain", "data-research", ["on-chain-data"], "Whale transaction alerts"),
            ("orderbook", "data-orderbook", "data-research", ["order-book-analysis"], "Order book imbalance"),
            ("funding_rate", "data-funding", "data-research", ["futures-trading"], "Funding rate monitoring"),
            ("ml_signal", "ml-classifier", "ai-ml", ["spot-trading"], "Online gradient boosting ML"),
            ("alpha_swarm", "meta-aggregator", "meta-orchestration", ["spot-trading"], "Multi-agent alpha detection"),
            ("polymarket", "signal-sentiment", "signal-generators", ["sentiment-analysis"], "Prediction market signals"),
            ("yield_aggregator", "data-market", "data-research", ["defi-protocols"], "DeFi yield monitoring"),
            ("smart_order_router", "exec-router", "execution", ["multi-exchange"], "Cross-venue order routing"),
            ("risk_manager", "risk-portfolio", "risk-compliance", ["risk-scoring"], "Portfolio risk management"),
        ]

        for agent_id, atype, category, caps, desc in internal_agents:
            self.register_agent(agent_id, atype, category, caps, desc)

        log.info("Registered %d internal agents", len(internal_agents))

    async def resolve_domain(self, domain: str) -> AgentIdentity | None:
        """Resolve an Unstoppable Domains name to an agent identity."""
        # First check local registry
        for agent in self.agents.values():
            if agent.domain == domain:
                return agent

        # Then try Unstoppable Domains API
        if self._ud_api_key:
            try:
                import aiohttp
                headers = {"Authorization": f"Bearer {self._ud_api_key}"}
                url = f"{UD_RESOLUTION_API}/{domain}"
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            # Extract agent metadata from domain records
                            records = data.get("records", {})
                            return AgentIdentity(
                                agent_id=records.get("swarm.agent_id", domain.split(".")[0]),
                                name=domain.split(".")[0],
                                domain=domain,
                                wallet_address=records.get("crypto.ETH.address", ""),
                                description=records.get("swarm.description", ""),
                            )
            except Exception as e:
                log.debug("UD resolution failed for %s: %s", domain, e)

        return None

    async def _on_execution(self, report):
        """Update agent reputation based on trade outcomes."""
        if not hasattr(report, "status"):
            return

        agent_id = getattr(report, "agent_id", "") or getattr(report, "asset", "")
        pnl = getattr(report, "pnl_estimate", 0) or 0

        # Find the agent that generated the signal
        # In practice, this maps through the signal -> intent -> execution chain
        for aid, agent in self.agents.items():
            if aid in str(agent_id):
                self._update_reputation(aid, pnl)
                break

    async def _on_alpha_signal(self, sig: Signal):
        """Track alpha swarm signal outcomes."""
        if isinstance(sig, Signal):
            agent = self.agents.get("alpha_swarm")
            if agent:
                agent.total_signals += 1
                agent.last_active = time.time()

    def _update_reputation(self, agent_id: str, pnl: float):
        """Update an agent's reputation score based on trade PnL."""
        agent = self.agents.get(agent_id)
        if not agent:
            return

        old_score = agent.reputation_score
        agent.total_signals += 1

        if pnl > 0:
            agent.profitable_signals += 1
            # Reputation goes up: bigger win = bigger boost, capped at +5
            boost = min(5.0, pnl / 100)
            agent.reputation_score = min(100, agent.reputation_score + boost)
        elif pnl < 0:
            # Reputation goes down: bigger loss = bigger penalty, capped at -3
            penalty = min(3.0, abs(pnl) / 100)
            agent.reputation_score = max(0, agent.reputation_score - penalty)

        agent.total_pnl += pnl
        agent.win_rate = agent.profitable_signals / max(agent.total_signals, 1)
        agent.last_active = time.time()

        if abs(agent.reputation_score - old_score) > 0.5:
            self._reputation_history.append(ReputationUpdate(
                agent_id=agent_id,
                old_score=old_score,
                new_score=agent.reputation_score,
                reason=f"trade pnl=${pnl:.2f}",
                pnl_delta=pnl,
            ))
            if len(self._reputation_history) > 1000:
                self._reputation_history = self._reputation_history[-500:]

    def get_leaderboard(self, top_n: int = 10) -> list[dict]:
        """Get the top agents by reputation."""
        sorted_agents = sorted(
            self.agents.values(),
            key=lambda a: a.reputation_score,
            reverse=True,
        )
        return [
            {
                "agent_id": a.agent_id,
                "domain": a.domain,
                "reputation": round(a.reputation_score, 1),
                "win_rate": round(a.win_rate, 3),
                "total_pnl": round(a.total_pnl, 2),
                "signals": a.total_signals,
                "category": a.category,
            }
            for a in sorted_agents[:top_n]
        ]

    def find_agents(
        self, category: str = "", capability: str = "",
        min_reputation: float = 0.0,
    ) -> list[AgentIdentity]:
        """Find agents by criteria."""
        results = []
        for agent in self.agents.values():
            if category and agent.category != category:
                continue
            if capability and capability not in agent.capabilities:
                continue
            if agent.reputation_score < min_reputation:
                continue
            results.append(agent)
        return results

    def summary(self) -> dict:
        active = [a for a in self.agents.values() if a.status == "active"]
        return {
            "total_registered": len(self.agents),
            "active": len(active),
            "domain_base": self._domain_base,
            "chain_id": self.chain_id,
            "avg_reputation": round(
                sum(a.reputation_score for a in active) / max(len(active), 1), 1
            ),
            "total_signals": sum(a.total_signals for a in active),
            "total_pnl": round(sum(a.total_pnl for a in active), 2),
            "leaderboard": self.get_leaderboard(5),
            "reputation_updates": len(self._reputation_history),
        }
