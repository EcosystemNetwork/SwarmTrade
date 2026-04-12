"""Agent automation layer: supervisor, health monitoring, scheduled tasks, and lifecycle management.

Provides:
- AgentSupervisor: tracks heartbeats, detects stalled agents, auto-restarts crashed tasks
- Scheduler: periodic health checks, circuit breaker resume, portfolio snapshots
- Lifecycle: ordered startup/shutdown sequencing for the full agent swarm
"""
from __future__ import annotations
import asyncio, logging, time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

from .core import Bus, ExecutionReport

log = logging.getLogger("swarm.auto")


# ---------------------------------------------------------------------------
# Agent heartbeat + health tracking
# ---------------------------------------------------------------------------
@dataclass
class AgentHealth:
    """Health record for a single agent."""
    name: str
    last_heartbeat: float = 0.0
    ticks: int = 0
    errors: int = 0
    restarts: int = 0
    status: str = "starting"  # starting | healthy | stale | dead | stopped

    def beat(self):
        self.last_heartbeat = time.time()
        self.ticks += 1
        self.status = "healthy"

    def age(self) -> float:
        if self.last_heartbeat == 0:
            return 0.0
        return time.time() - self.last_heartbeat


class AgentSupervisor:
    """Monitors agent health via heartbeats and restarts crashed async tasks.

    Usage:
        sup = AgentSupervisor(bus)
        sup.register("orderbook", ob.run, stale_after=15.0)
        sup.register("scout", scout.run, stale_after=10.0)
        await sup.run()  # runs forever, checking health every interval
    """

    def __init__(self, bus: Bus, check_interval: float = 5.0,
                 max_restarts: int = 3):
        self.bus = bus
        self.check_interval = check_interval
        self.max_restarts = max_restarts
        self._agents: dict[str, AgentHealth] = {}
        self._factories: dict[str, Callable[[], Awaitable[None]]] = {}
        self._stale_thresholds: dict[str, float] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stoppables: dict[str, Any] = {}  # objects with .stop()
        self._stop = False

        bus.subscribe("agent.heartbeat", self._on_heartbeat)

    def register(self, name: str, factory: Callable[[], Awaitable[None]],
                 stale_after: float = 30.0, stoppable: Any = None):
        """Register an agent for supervision.

        Args:
            name: unique agent identifier
            factory: async callable that runs the agent (e.g. agent.run)
            stale_after: seconds without heartbeat before marking stale
            stoppable: object with .stop() method for graceful shutdown
        """
        self._agents[name] = AgentHealth(name=name)
        self._factories[name] = factory
        self._stale_thresholds[name] = stale_after
        if stoppable is not None:
            self._stoppables[name] = stoppable

    def heartbeat(self, name: str):
        """Record a heartbeat for an agent (call from agent code)."""
        if name in self._agents:
            self._agents[name].beat()

    async def _on_heartbeat(self, payload: dict):
        name = payload.get("agent")
        if name and name in self._agents:
            self._agents[name].beat()

    def start_all(self) -> list[asyncio.Task]:
        """Launch all registered agents. Returns list of tasks for external tracking."""
        tasks = []
        for name, factory in self._factories.items():
            task = asyncio.create_task(self._supervised_run(name, factory))
            self._tasks[name] = task
            tasks.append(task)
            self._agents[name].status = "starting"
            log.info("SUPERVISOR started: %s", name)
        return tasks

    async def _supervised_run(self, name: str, factory: Callable[[], Awaitable[None]]):
        """Wrap an agent's run() with crash detection and auto-restart."""
        health = self._agents[name]
        while not self._stop and health.restarts <= self.max_restarts:
            try:
                health.status = "healthy"
                health.beat()
                await factory()
                # Normal exit (agent stopped itself)
                health.status = "stopped"
                return
            except asyncio.CancelledError:
                health.status = "stopped"
                return
            except Exception as e:
                health.errors += 1
                health.restarts += 1
                health.status = "dead"
                log.error("SUPERVISOR %s crashed (%d/%d): %s",
                          name, health.restarts, self.max_restarts, e)
                if health.restarts <= self.max_restarts:
                    # Backoff: 2^restarts seconds, capped at 30s
                    delay = min(30.0, 2 ** health.restarts)
                    log.info("SUPERVISOR restarting %s in %.0fs", name, delay)
                    await asyncio.sleep(delay)
                else:
                    log.critical("SUPERVISOR %s exceeded max restarts, giving up", name)
                    await self.bus.publish("safety.agent_dead", {"agent": name, "errors": health.errors})

    async def run(self):
        """Periodic health check loop."""
        while not self._stop:
            await asyncio.sleep(self.check_interval)
            await self._check_health()

    async def _check_health(self):
        """Check all agents for staleness."""
        now = time.time()
        for name, health in self._agents.items():
            if health.status in ("stopped", "dead"):
                continue
            threshold = self._stale_thresholds.get(name, 30.0)
            if health.last_heartbeat > 0 and health.age() > threshold:
                if health.status != "stale":
                    health.status = "stale"
                    log.warning("SUPERVISOR %s is stale (%.0fs since last heartbeat)",
                                name, health.age())
                    await self.bus.publish("agent.stale", {"agent": name, "age": health.age()})

    def stop_all(self):
        """Signal all agents to stop."""
        self._stop = True
        for name, stoppable in self._stoppables.items():
            try:
                stoppable.stop()
            except Exception as e:
                log.warning("SUPERVISOR stop %s failed: %s", name, e)
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()

    def health_report(self) -> dict:
        """JSON-serializable health report for all agents."""
        return {
            name: {
                "status": h.status,
                "ticks": h.ticks,
                "errors": h.errors,
                "restarts": h.restarts,
                "age_s": round(h.age(), 1),
            }
            for name, h in self._agents.items()
        }


# ---------------------------------------------------------------------------
# Scheduled automation tasks
# ---------------------------------------------------------------------------
class Scheduler:
    """Runs periodic automation tasks on configurable intervals.

    Built-in tasks:
    - Circuit breaker resume check
    - Portfolio snapshot publication
    - Agent health summary
    - Weight decay (prevents overfitting to recent trades)
    """

    def __init__(self, bus: Bus, supervisor: AgentSupervisor | None = None):
        self.bus = bus
        self.supervisor = supervisor
        self._tasks: list[tuple[str, float, Callable[[], Awaitable[None]]]] = []
        self._stop = False

    def every(self, name: str, interval_s: float,
              fn: Callable[[], Awaitable[None]]):
        """Register a periodic task."""
        self._tasks.append((name, interval_s, fn))

    async def run(self):
        """Launch all scheduled tasks concurrently."""
        coros = [self._loop(name, interval, fn)
                 for name, interval, fn in self._tasks]
        await asyncio.gather(*coros, return_exceptions=True)

    async def _loop(self, name: str, interval: float,
                    fn: Callable[[], Awaitable[None]]):
        while not self._stop:
            await asyncio.sleep(interval)
            try:
                await fn()
            except asyncio.CancelledError:
                return
            except Exception as e:
                log.error("SCHEDULER %s failed: %s", name, e)

    def stop(self):
        self._stop = True


def build_scheduler(
    bus: Bus,
    supervisor: AgentSupervisor | None = None,
    circuit_breaker=None,
    strategist=None,
    portfolio=None,
    state: dict | None = None,
) -> Scheduler:
    """Wire up the standard automation schedule."""
    sched = Scheduler(bus, supervisor)

    # --- Circuit breaker auto-resume (every 30s) ---
    if circuit_breaker is not None:
        async def _cb_resume():
            await circuit_breaker.check_resume()
        sched.every("cb_resume", 30.0, _cb_resume)

    # --- Health summary (every 60s) ---
    if supervisor is not None:
        async def _health():
            report = supervisor.health_report()
            stale = [n for n, h in report.items() if h["status"] == "stale"]
            dead = [n for n, h in report.items() if h["status"] == "dead"]
            if stale or dead:
                log.warning("HEALTH stale=%s dead=%s", stale, dead)
            await bus.publish("automation.health", report)
        sched.every("health", 60.0, _health)

    # --- Portfolio snapshot (every 30s) ---
    if portfolio is not None:
        async def _portfolio():
            summary = portfolio.summary()
            await bus.publish("automation.portfolio", summary)
        sched.every("portfolio_snap", 30.0, _portfolio)

    # --- Weight decay (every 120s) — prevents overfitting ---
    if strategist is not None:
        async def _weight_decay():
            """Gently decay weights back toward defaults to prevent runaway drift."""
            decay = 0.02  # 2% pull toward default each cycle
            for agent, default_w in strategist.DEFAULT_WEIGHTS.items():
                if agent in strategist.weights:
                    current = strategist.weights[agent]
                    strategist.weights[agent] = current + decay * (default_w - current)
            # Renormalize
            total = sum(strategist.weights.values()) or 1.0
            for k in strategist.weights:
                strategist.weights[k] /= total
        sched.every("weight_decay", 120.0, _weight_decay)

    # --- Daily PnL reset at midnight (check every 300s) ---
    if state is not None:
        _last_day = {"val": _today_ordinal()}

        async def _daily_reset():
            today = _today_ordinal()
            if today != _last_day["val"]:
                old_pnl = state.get("daily_pnl", 0.0)
                log.info("DAILY RESET pnl=%.4f trades=%d",
                         old_pnl, state.get("trade_count", 0))
                await bus.publish("automation.daily_reset", {
                    "date": _last_day["val"],
                    "pnl": old_pnl,
                    "trades": state.get("trade_count", 0),
                })
                state["daily_pnl"] = 0.0
                state["trade_count"] = 0
                _last_day["val"] = today
        sched.every("daily_reset", 300.0, _daily_reset)

    return sched


def _today_ordinal() -> int:
    import datetime
    return datetime.datetime.utcnow().toordinal()


# ---------------------------------------------------------------------------
# Heartbeat mixin for agents
# ---------------------------------------------------------------------------
class HeartbeatMixin:
    """Mixin that publishes heartbeats on the bus. Add to any agent with a bus attr."""

    async def _heartbeat(self):
        if hasattr(self, "bus") and hasattr(self, "name"):
            await self.bus.publish("agent.heartbeat", {"agent": self.name})
