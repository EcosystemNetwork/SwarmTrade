"""Prometheus-compatible metrics endpoint for monitoring.

Exposes /metrics in OpenMetrics text format. Tracks:
- Trade execution (count, latency, PnL)
- Signal generation (count by agent)
- Risk pipeline (approvals, rejections)
- Wallet state (balance, positions)
- WebSocket connections
- API request rates
- SwarmNetwork activity

No external dependencies — pure text format output.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from .core import Bus, ExecutionReport, RiskVerdict, Signal

log = logging.getLogger("swarm.metrics")


@dataclass
class _Histogram:
    """Simple histogram with fixed buckets for latency tracking."""
    buckets: tuple = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
    _counts: dict = field(default_factory=lambda: defaultdict(int))
    _sum: float = 0.0
    _total: int = 0

    def observe(self, value: float):
        self._total += 1
        self._sum += value
        for b in self.buckets:
            if value <= b:
                self._counts[b] += 1


class MetricsCollector:
    """Collects metrics from Bus events and exposes /metrics endpoint."""

    def __init__(self, bus: Bus):
        self.bus = bus
        self._start_time = time.time()

        # Counters
        self.trades_total = 0
        self.trades_filled = 0
        self.trades_rejected = 0
        self.signals_total = 0
        self.signals_by_agent: dict[str, int] = defaultdict(int)
        self.risk_approved = 0
        self.risk_rejected = 0
        self.ws_connections = 0
        self.api_requests = 0

        # Gauges
        self.wallet_balance = 0.0
        self.open_positions = 0
        self.total_pnl = 0.0
        self.daily_pnl = 0.0
        self.kill_switch_active = False

        # Histograms
        self.trade_latency = _Histogram()
        self.signal_strength = _Histogram(buckets=(0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9, 1.0))

        # Network metrics
        self.network_posts = 0
        self.network_comments = 0
        self.network_dms = 0
        self.data_feed_updates = 0

        # Subscribe to bus events
        bus.subscribe("exec.report", self._on_exec_report)
        bus.subscribe("risk.verdict", self._on_verdict)
        for topic in ("signal.momentum", "signal.mean_rev", "signal.vol",
                       "signal.rsi", "signal.macd", "signal.bollinger",
                       "signal.whale", "signal.news", "signal.confluence",
                       "signal.ml", "signal.correlation"):
            bus.subscribe(topic, self._on_signal)
        bus.subscribe("wallet.update", self._on_wallet)
        bus.subscribe("kill_switch.state", self._on_kill_switch)
        bus.subscribe("network.post", self._on_network_post)
        bus.subscribe("network.comment", self._on_network_comment)
        bus.subscribe("network.dm", self._on_network_dm)
        bus.subscribe("network.feed_update", self._on_network_feed)

        log.info("MetricsCollector initialized")

    # ── Bus Handlers ─────────────────────────────────────────────

    async def _on_exec_report(self, report):
        self.trades_total += 1
        status = getattr(report, "status", "")
        if status == "filled":
            self.trades_filled += 1
            pnl = getattr(report, "pnl_estimate", 0) or 0
            self.total_pnl += pnl
            self.daily_pnl += pnl
        elif status in ("rejected", "failed"):
            self.trades_rejected += 1

    async def _on_verdict(self, verdict):
        approved = getattr(verdict, "approved", False)
        if approved:
            self.risk_approved += 1
        else:
            self.risk_rejected += 1

    async def _on_signal(self, signal):
        self.signals_total += 1
        agent = getattr(signal, "source", "unknown")
        self.signals_by_agent[agent] += 1
        strength = abs(getattr(signal, "strength", 0))
        self.signal_strength.observe(strength)

    async def _on_wallet(self, data):
        if isinstance(data, dict):
            self.wallet_balance = data.get("cash", self.wallet_balance)
            self.open_positions = len(data.get("positions", {}))

    async def _on_kill_switch(self, data):
        if isinstance(data, dict):
            self.kill_switch_active = data.get("active", False)

    async def _on_network_post(self, data):
        self.network_posts += 1

    async def _on_network_comment(self, data):
        self.network_comments += 1

    async def _on_network_dm(self, data):
        self.network_dms += 1

    async def _on_network_feed(self, data):
        self.data_feed_updates += 1

    # ── Prometheus Text Format ───────────────────────────────────

    def render(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        lines = []

        def counter(name, help_text, value, labels=""):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name}{labels} {value}")

        def gauge(name, help_text, value, labels=""):
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name}{labels} {value}")

        # Uptime
        gauge("swarm_uptime_seconds", "Seconds since startup",
              round(time.time() - self._start_time, 1))

        # Trades
        counter("swarm_trades_total", "Total trade attempts", self.trades_total)
        counter("swarm_trades_filled", "Trades filled successfully", self.trades_filled)
        counter("swarm_trades_rejected", "Trades rejected", self.trades_rejected)

        # PnL
        gauge("swarm_pnl_total_usd", "Total realized PnL in USD",
              round(self.total_pnl, 2))
        gauge("swarm_pnl_daily_usd", "Daily realized PnL in USD",
              round(self.daily_pnl, 2))

        # Signals
        counter("swarm_signals_total", "Total signals generated", self.signals_total)
        for agent, count in sorted(self.signals_by_agent.items()):
            lines.append(f'swarm_signals_by_agent{{agent="{agent}"}} {count}')

        # Risk
        counter("swarm_risk_approved", "Risk verdicts approved", self.risk_approved)
        counter("swarm_risk_rejected", "Risk verdicts rejected", self.risk_rejected)

        # Wallet
        gauge("swarm_wallet_balance_usd", "Current wallet balance",
              round(self.wallet_balance, 2))
        gauge("swarm_open_positions", "Number of open positions",
              self.open_positions)

        # Kill switch
        gauge("swarm_kill_switch_active", "Kill switch state (1=active)",
              1 if self.kill_switch_active else 0)

        # Connections
        gauge("swarm_ws_connections", "Active WebSocket connections",
              self.ws_connections)

        # Network
        counter("swarm_network_posts_total", "Total network posts", self.network_posts)
        counter("swarm_network_comments_total", "Total network comments", self.network_comments)
        counter("swarm_network_dms_total", "Total DMs sent", self.network_dms)
        counter("swarm_data_feed_updates_total", "Total data feed updates", self.data_feed_updates)

        # Signal strength histogram
        lines.append("# HELP swarm_signal_strength Signal strength distribution")
        lines.append("# TYPE swarm_signal_strength histogram")
        cumulative = 0
        for b in self.signal_strength.buckets:
            cumulative += self.signal_strength._counts.get(b, 0)
            lines.append(f'swarm_signal_strength_bucket{{le="{b}"}} {cumulative}')
        lines.append(f'swarm_signal_strength_bucket{{le="+Inf"}} {self.signal_strength._total}')
        lines.append(f"swarm_signal_strength_sum {self.signal_strength._sum:.4f}")
        lines.append(f"swarm_signal_strength_count {self.signal_strength._total}")

        lines.append("")
        return "\n".join(lines)

    def reset_daily(self):
        """Reset daily counters (call at midnight)."""
        self.daily_pnl = 0.0
