"""Agent Factory — dynamic agent spawning and lifecycle management.

Inspired by Nimble (Agent Factory contract for on-chain agent spawning),
AgentHive (orchestrator hires specialists dynamically), and Colony
(plugin-based agent extension).

Creates new agent instances on-demand based on market conditions:
  1. Detect opportunity requiring specialized analysis
  2. Spawn a temporary agent with focused parameters
  3. Agent runs, produces signals, earns ELO
  4. Agent auto-terminates after TTL or when opportunity closes
  5. High-performing temporary agents get promoted to permanent

Also manages agent lifecycle: health checks, restart on failure,
graceful shutdown, and resource limits.

Bus integration:
  Subscribes to: alpha.discovered (spawn specialist for new opportunities)
  Publishes to:  factory.spawned, factory.terminated, factory.promoted

No external dependencies.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .core import Bus, Signal

log = logging.getLogger("swarm.factory")


@dataclass
class AgentBlueprint:
    """Template for spawning new agents."""
    blueprint_id: str
    name: str
    agent_type: str         # "signal", "risk", "execution", "analysis"
    # Configuration
    signal_sources: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)
    parameters: dict = field(default_factory=dict)
    # Resource limits
    max_signals_per_hour: int = 60
    max_memory_mb: int = 256
    ttl_seconds: float = 3600.0    # auto-terminate after 1 hour
    # Promotion criteria
    min_elo_for_promotion: float = 600.0
    min_signals_for_promotion: int = 20


@dataclass
class SpawnedAgent:
    """A dynamically spawned agent instance."""
    instance_id: str
    blueprint_id: str
    name: str
    # Status
    status: str = "running"     # running, stopped, promoted, failed
    signals_produced: int = 0
    elo: float = 500.0
    # Lifecycle
    spawned_at: float = field(default_factory=time.time)
    ttl_seconds: float = 3600.0
    last_signal: float = 0.0
    last_health_check: float = field(default_factory=time.time)
    # Performance
    profitable_signals: int = 0
    total_pnl_contribution: float = 0.0

    @property
    def age_seconds(self) -> float:
        return time.time() - self.spawned_at

    @property
    def is_expired(self) -> bool:
        return self.age_seconds > self.ttl_seconds

    @property
    def is_healthy(self) -> bool:
        if self.status != "running":
            return False
        stale = time.time() - self.last_health_check > 120
        return not stale


class AgentFactory:
    """Dynamically spawns and manages agent lifecycle.

    Default blueprints cover common opportunity types:
      - momentum_specialist: spawns when strong trend detected
      - volatility_specialist: spawns during high-vol regimes
      - arb_specialist: spawns when cross-DEX spread widens
      - narrative_analyst: spawns when narrative engine detects pattern
    """

    DEFAULT_BLUEPRINTS = {
        "momentum_specialist": AgentBlueprint(
            blueprint_id="bp-momentum",
            name="Momentum Specialist",
            agent_type="signal",
            signal_sources=["momentum", "macd", "rsi"],
            parameters={"lookback": 20, "threshold": 0.6},
            ttl_seconds=1800,  # 30 min for momentum plays
        ),
        "volatility_specialist": AgentBlueprint(
            blueprint_id="bp-volatility",
            name="Volatility Specialist",
            agent_type="signal",
            signal_sources=["bollinger", "atr_trailing", "options"],
            parameters={"vol_threshold": 0.8},
            ttl_seconds=3600,
        ),
        "arb_specialist": AgentBlueprint(
            blueprint_id="bp-arb",
            name="Arbitrage Specialist",
            agent_type="execution",
            signal_sources=["arbitrage", "orderbook"],
            parameters={"min_spread_bps": 20},
            ttl_seconds=900,  # 15 min for arb windows
        ),
        "narrative_analyst": AgentBlueprint(
            blueprint_id="bp-narrative",
            name="Narrative Analyst",
            agent_type="analysis",
            signal_sources=["whale", "sentiment", "news"],
            parameters={"depth": "deep"},
            ttl_seconds=7200,  # 2 hours for narrative analysis
        ),
    }

    def __init__(self, bus: Bus, max_concurrent: int = 10):
        self.bus = bus
        self.max_concurrent = max_concurrent
        self._blueprints: dict[str, AgentBlueprint] = dict(self.DEFAULT_BLUEPRINTS)
        self._agents: dict[str, SpawnedAgent] = {}
        self._instance_counter = 0
        self._stats = {
            "spawned": 0, "terminated": 0, "promoted": 0, "failed": 0,
        }

        bus.subscribe("alpha.discovered", self._on_alpha)
        bus.subscribe("signal.regime", self._on_regime)

    def register_blueprint(self, name: str, blueprint: AgentBlueprint):
        """Register a new agent blueprint."""
        self._blueprints[name] = blueprint
        log.info("Factory: blueprint registered — %s", name)

    async def spawn(self, blueprint_name: str, assets: list[str] | None = None,
                    reason: str = "") -> SpawnedAgent | None:
        """Spawn a new agent from a blueprint."""
        # Check concurrency limit
        active = [a for a in self._agents.values() if a.status == "running"]
        if len(active) >= self.max_concurrent:
            log.warning("Factory: max concurrent agents reached (%d)", self.max_concurrent)
            return None

        bp = self._blueprints.get(blueprint_name)
        if not bp:
            log.warning("Factory: unknown blueprint '%s'", blueprint_name)
            return None

        self._instance_counter += 1
        instance_id = f"spawn-{self._instance_counter:05d}"

        agent = SpawnedAgent(
            instance_id=instance_id,
            blueprint_id=bp.blueprint_id,
            name=f"{bp.name} #{self._instance_counter}",
            ttl_seconds=bp.ttl_seconds,
        )
        self._agents[instance_id] = agent
        self._stats["spawned"] += 1

        log.info(
            "FACTORY SPAWN: %s (%s) for %s | TTL=%ds | reason: %s",
            instance_id, bp.name, assets or "general",
            bp.ttl_seconds, reason or "auto",
        )

        await self.bus.publish("factory.spawned", {
            "agent": agent, "blueprint": bp, "reason": reason,
        })

        return agent

    async def _on_alpha(self, msg: dict):
        """Auto-spawn specialists when alpha opportunities are discovered."""
        opp = msg.get("opportunity")
        if not opp:
            return

        # Decide which blueprint based on opportunity characteristics
        signals = msg.get("supporting_signals", [])
        signal_agents = [s.get("agent_id", "") for s in signals] if isinstance(signals, list) else []

        if any("momentum" in a for a in signal_agents):
            await self.spawn("momentum_specialist", [opp.asset],
                           f"alpha opportunity: {opp.direction} {opp.asset}")
        elif any("arb" in a for a in signal_agents):
            await self.spawn("arb_specialist", [opp.asset],
                           f"arb opportunity: {opp.asset}")

    async def _on_regime(self, sig: Signal):
        """Spawn specialists based on regime changes."""
        if not isinstance(sig, Signal):
            return
        rationale = sig.rationale.lower()
        if "volatile" in rationale or "crisis" in rationale:
            active_vol = [a for a in self._agents.values()
                         if "Volatility" in a.name and a.status == "running"]
            if not active_vol:
                await self.spawn("volatility_specialist", [sig.asset],
                               f"regime change: {rationale[:50]}")

    async def check_lifecycle(self):
        """Check all agents for expiry, health, and promotion."""
        for agent in list(self._agents.values()):
            if agent.status != "running":
                continue

            # TTL expiry
            if agent.is_expired:
                await self._terminate(agent, "ttl_expired")
                continue

            # Promotion check
            bp = self._blueprints.get(agent.blueprint_id.replace("bp-", ""), None)
            if bp and agent.elo >= bp.min_elo_for_promotion and \
               agent.signals_produced >= bp.min_signals_for_promotion:
                await self._promote(agent)

    async def _terminate(self, agent: SpawnedAgent, reason: str):
        agent.status = "stopped"
        self._stats["terminated"] += 1
        log.info("FACTORY TERMINATE: %s (%s) — %s (elo=%.0f, signals=%d)",
                 agent.instance_id, agent.name, reason,
                 agent.elo, agent.signals_produced)
        await self.bus.publish("factory.terminated", {
            "agent": agent, "reason": reason,
        })

    async def _promote(self, agent: SpawnedAgent):
        agent.status = "promoted"
        self._stats["promoted"] += 1
        log.info(
            "FACTORY PROMOTED: %s (%s) — elo=%.0f, signals=%d, pnl=$%.4f",
            agent.instance_id, agent.name, agent.elo,
            agent.signals_produced, agent.total_pnl_contribution,
        )
        await self.bus.publish("factory.promoted", {"agent": agent})

    def summary(self) -> dict:
        return {
            **self._stats,
            "blueprints": len(self._blueprints),
            "active": len([a for a in self._agents.values() if a.status == "running"]),
            "agents": [
                {
                    "id": a.instance_id,
                    "name": a.name,
                    "status": a.status,
                    "elo": round(a.elo, 0),
                    "signals": a.signals_produced,
                    "age_min": round(a.age_seconds / 60, 1),
                }
                for a in self._agents.values()
            ][-10:],
        }
