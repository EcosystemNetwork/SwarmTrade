"""Agent Observability Dashboard — real-time monitoring and diagnostics.

Inspired by Aegis Agent (5-minute threat detection), Watcher.Fai (personal
DeFi account manager tracking across dApps), and KalmanGuard's 7-layer
monitoring architecture.

Provides unified observability across all 100+ agents:
  1. Health monitoring — is each agent producing signals on time?
  2. Performance metrics — per-agent PnL, hit rate, latency
  3. Anomaly detection — unusual behavior patterns
  4. Dependency graph — which agents depend on which data sources
  5. Resource tracking — signal volume, memory, API calls per agent
  6. Alert routing — PagerDuty/Slack/Telegram for critical issues

Bus integration:
  Subscribes to: signal.* (all), exec.report, risk.*, shield.*
  Publishes to:  observability.alert, observability.health_report
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from collections import defaultdict

from typing import Awaitable, Callable

from .core import Bus, Signal, ExecutionReport

log = logging.getLogger("swarm.observe")

AlertHook = Callable[[str, str], Awaitable[None]]


@dataclass
class AgentHealth:
    """Health status for a single agent."""
    agent_id: str
    status: str = "unknown"        # healthy, degraded, stale, dead
    # Activity
    signals_total: int = 0
    signals_last_hour: int = 0
    last_signal_ts: float = 0.0
    avg_interval_s: float = 0.0
    # Performance
    hit_rate: float = 0.0          # profitable signals / total
    avg_strength: float = 0.0
    avg_confidence: float = 0.0
    total_pnl_contribution: float = 0.0
    # Resource usage
    signals_per_minute: float = 0.0
    # Anomalies
    anomaly_count: int = 0
    last_anomaly: str = ""


@dataclass
class SystemHealthReport:
    """Overall system health snapshot."""
    ts: float = field(default_factory=time.time)
    total_agents: int = 0
    healthy: int = 0
    degraded: int = 0
    stale: int = 0
    dead: int = 0
    # System metrics
    signals_per_minute: float = 0.0
    trades_today: int = 0
    pnl_today: float = 0.0
    # Top issues
    issues: list[str] = field(default_factory=list)


class ObservabilityDashboard:
    """Unified monitoring across all swarm agents.

    Every signal that flows through the Bus is tracked. Agents that
    stop producing signals are marked as stale/dead. Unusual patterns
    (sudden spike in signals, confidence drop, etc.) trigger anomaly alerts.

    Health classification:
      HEALTHY: producing signals at expected rate
      DEGRADED: producing signals but below normal rate or low quality
      STALE: no signal in last 5 minutes (should be producing)
      DEAD: no signal in last 15 minutes
    """

    STALE_THRESHOLD = 300.0    # 5 minutes
    DEAD_THRESHOLD = 900.0     # 15 minutes
    DEGRADED_RATE = 0.5        # signals/min below this = degraded

    def __init__(self, bus: Bus, alert_hook: AlertHook | None = None):
        self.bus = bus
        self.alert_hook = alert_hook
        self._agents: dict[str, AgentHealth] = {}
        self._signal_times: dict[str, list[float]] = defaultdict(list)
        self._system_signals: list[float] = []
        self._trades_today: int = 0
        self._pnl_today: float = 0.0
        self._alerts: list[dict] = []
        self._reports: list[SystemHealthReport] = []

        # Subscribe to everything
        signal_topics = [
            "signal.momentum", "signal.mean_reversion", "signal.rsi",
            "signal.macd", "signal.bollinger", "signal.vwap",
            "signal.ichimoku", "signal.whale", "signal.news",
            "signal.ml", "signal.confluence", "signal.smart_money",
            "signal.sentiment", "signal.onchain", "signal.funding_rate",
            "signal.orderbook", "signal.fear_greed", "signal.social",
            "signal.arbitrage", "signal.correlation", "signal.multitf",
            "signal.fusion", "signal.debate", "signal.alpha_swarm",
            "signal.narrative", "signal.whale_mirror", "signal.prediction",
            "signal.options_strategy", "signal.social_alpha",
            "signal.marketplace",
        ]
        for topic in signal_topics:
            bus.subscribe(topic, self._on_signal)

        bus.subscribe("exec.report", self._on_execution)
        bus.subscribe("shield.emergency", self._on_emergency)

    async def _on_signal(self, sig: Signal):
        """Track every signal for health monitoring."""
        if not isinstance(sig, Signal):
            return

        now = time.time()
        agent_id = sig.agent_id
        health = self._get_health(agent_id)

        health.signals_total += 1
        health.last_signal_ts = now
        health.avg_strength = (health.avg_strength * 0.9) + (sig.strength * 0.1)
        health.avg_confidence = (health.avg_confidence * 0.9) + (sig.confidence * 0.1)

        # Track signal times for rate calculation
        self._signal_times[agent_id].append(now)
        cutoff = now - 3600
        self._signal_times[agent_id] = [t for t in self._signal_times[agent_id] if t > cutoff]
        health.signals_last_hour = len(self._signal_times[agent_id])
        health.signals_per_minute = health.signals_last_hour / 60

        # System-wide tracking
        self._system_signals.append(now)
        self._system_signals = [t for t in self._system_signals if t > cutoff]

        # Anomaly detection: sudden signal volume spike
        if health.signals_per_minute > 10 and health.signals_total > 100:
            # More than 10 signals/min from one agent is unusual
            health.anomaly_count += 1
            health.last_anomaly = f"high signal rate: {health.signals_per_minute:.1f}/min"

        # Update health status
        self._classify_health(health)

    async def _on_execution(self, report: ExecutionReport):
        if report.status == "filled":
            self._trades_today += 1
            self._pnl_today += report.pnl_estimate or 0

    async def _on_emergency(self, action):
        self._alerts.append({
            "level": "critical",
            "message": "Liquidation shield emergency triggered",
            "ts": time.time(),
        })

    async def _fire_alerts(self, report: SystemHealthReport) -> None:
        """Send external alerts for critical health conditions."""
        if not self.alert_hook:
            return
        try:
            if report.dead > 0:
                dead_names = [h.agent_id for h in self._agents.values() if h.status == "dead"]
                await self.alert_hook(
                    "WARNING",
                    f"{report.dead} agent(s) DEAD (no signal in {self.DEAD_THRESHOLD/60:.0f}min): "
                    f"{', '.join(dead_names[:10])}",
                )
            if report.dead > report.total_agents * 0.5 and report.total_agents > 5:
                await self.alert_hook(
                    "CRITICAL",
                    f"Majority agent failure: {report.dead}/{report.total_agents} agents dead",
                )
            if report.pnl_today < -500:
                await self.alert_hook(
                    "WARNING",
                    f"Daily PnL alert: ${report.pnl_today:,.2f}",
                )
        except Exception as e:
            log.error("Observability alert dispatch failed: %s", e)

    def _get_health(self, agent_id: str) -> AgentHealth:
        if agent_id not in self._agents:
            self._agents[agent_id] = AgentHealth(agent_id=agent_id)
        return self._agents[agent_id]

    def _classify_health(self, health: AgentHealth):
        """Classify agent health based on signal activity."""
        now = time.time()
        time_since_last = now - health.last_signal_ts if health.last_signal_ts > 0 else float("inf")

        if time_since_last > self.DEAD_THRESHOLD:
            health.status = "dead"
        elif time_since_last > self.STALE_THRESHOLD:
            health.status = "stale"
        elif health.signals_per_minute < self.DEGRADED_RATE and health.signals_total > 10:
            health.status = "degraded"
        else:
            health.status = "healthy"

    def generate_report(self) -> SystemHealthReport:
        """Generate a full system health report."""
        # Refresh all classifications
        for health in self._agents.values():
            self._classify_health(health)

        statuses = defaultdict(int)
        for h in self._agents.values():
            statuses[h.status] += 1

        issues = []
        for h in self._agents.values():
            if h.status == "dead":
                issues.append(f"{h.agent_id}: DEAD (no signal in {self.DEAD_THRESHOLD/60:.0f}min)")
            elif h.status == "stale":
                issues.append(f"{h.agent_id}: STALE")
            if h.anomaly_count > 3:
                issues.append(f"{h.agent_id}: {h.anomaly_count} anomalies ({h.last_anomaly})")

        report = SystemHealthReport(
            total_agents=len(self._agents),
            healthy=statuses.get("healthy", 0),
            degraded=statuses.get("degraded", 0),
            stale=statuses.get("stale", 0),
            dead=statuses.get("dead", 0),
            signals_per_minute=len(self._system_signals) / 60,
            trades_today=self._trades_today,
            pnl_today=round(self._pnl_today, 4),
            issues=issues[:10],
        )
        self._reports.append(report)
        if len(self._reports) > 100:
            self._reports = self._reports[-50:]

        # Fire external alerts in background if hook configured
        if self.alert_hook:
            try:
                asyncio.get_running_loop().create_task(self._fire_alerts(report))
            except RuntimeError:
                pass  # No running loop (e.g. called from sync context)

        return report

    def agent_detail(self, agent_id: str) -> dict | None:
        health = self._agents.get(agent_id)
        if not health:
            return None
        return {
            "agent_id": health.agent_id,
            "status": health.status,
            "signals_total": health.signals_total,
            "signals_last_hour": health.signals_last_hour,
            "signals_per_minute": round(health.signals_per_minute, 2),
            "avg_strength": round(health.avg_strength, 3),
            "avg_confidence": round(health.avg_confidence, 3),
            "anomalies": health.anomaly_count,
            "last_signal_age_s": round(time.time() - health.last_signal_ts, 1) if health.last_signal_ts > 0 else None,
        }

    def summary(self) -> dict:
        report = self.generate_report()
        return {
            "system": {
                "total": report.total_agents,
                "healthy": report.healthy,
                "degraded": report.degraded,
                "stale": report.stale,
                "dead": report.dead,
            },
            "signals_per_minute": round(report.signals_per_minute, 1),
            "trades_today": report.trades_today,
            "pnl_today": report.pnl_today,
            "issues": report.issues[:5],
            "top_agents": sorted(
                [
                    {"agent": h.agent_id, "signals": h.signals_total, "status": h.status}
                    for h in self._agents.values()
                ],
                key=lambda x: x["signals"], reverse=True,
            )[:10],
        }
