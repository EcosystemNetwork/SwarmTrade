"""Agent memory system — persistent cross-session learning.

Inspired by OpenTradex's SOUL.md + strategy_notes pattern, adapted for Swarm Trade's
multi-agent architecture. The swarm records lessons after each session and reads them
back at startup so it doesn't repeat mistakes.

Files:
  SOUL.md           — immutable identity & risk rules (read-only)
  strategy_notes.md — append-only agent-written lessons (read+write)
  session_log.json  — structured session history for analysis
"""
from __future__ import annotations
import json, logging, time
from datetime import datetime, timezone
from pathlib import Path
from .core import Bus, ExecutionReport, PortfolioTracker

log = logging.getLogger("swarm.memory")

DEFAULT_NOTES_PATH = Path("strategy_notes.md")
DEFAULT_SESSION_LOG = Path("session_log.json")
DEFAULT_SOUL_PATH = Path("SOUL.md")


class AgentMemory:
    """Reads the SOUL at startup, records strategy notes after each session.

    Subscribes to bus events to accumulate session stats, then writes a
    summary to strategy_notes.md on shutdown.
    """

    def __init__(
        self,
        bus: Bus,
        portfolio: PortfolioTracker,
        state: dict,
        soul_path: Path = DEFAULT_SOUL_PATH,
        notes_path: Path = DEFAULT_NOTES_PATH,
        session_log_path: Path = DEFAULT_SESSION_LOG,
    ):
        self.bus = bus
        self.portfolio = portfolio
        self.state = state
        self.soul_path = soul_path
        self.notes_path = notes_path
        self.session_log_path = session_log_path

        # Session accumulators
        self.trades: list[dict] = []
        self.regimes_seen: list[str] = []
        self.circuit_breaker_events: list[dict] = []
        self.risk_rejections: list[dict] = []
        self.start_time = time.time()
        self.start_equity = portfolio.total_equity()
        self._thought_stream: list[dict] = []  # live thought log

        # Subscribe to bus events
        bus.subscribe("exec.report", self._on_report)
        bus.subscribe("signal.regime", self._on_regime)
        bus.subscribe("safety.halt", self._on_halt)
        bus.subscribe("risk.verdict", self._on_verdict)

    # ── Soul access ────────────────────────────────────────────

    def read_soul(self) -> str:
        """Read the SOUL.md file. Returns empty string if not found."""
        if self.soul_path.exists():
            text = self.soul_path.read_text()
            log.info("SOUL loaded (%d chars)", len(text))
            return text
        log.warning("SOUL.md not found at %s", self.soul_path)
        return ""

    def read_notes(self) -> str:
        """Read accumulated strategy notes from prior sessions."""
        if self.notes_path.exists():
            text = self.notes_path.read_text()
            log.info("Strategy notes loaded (%d lines)", text.count("\n"))
            return text
        return ""

    def get_past_sessions(self, limit: int = 10) -> list[dict]:
        """Load recent session summaries for context."""
        if not self.session_log_path.exists():
            return []
        try:
            data = json.loads(self.session_log_path.read_text())
            return data[-limit:]
        except (json.JSONDecodeError, KeyError):
            return []

    # ── Live thought stream ────────────────────────────────────

    def record_thought(self, agent: str, thought: str, category: str = "observation"):
        """Record an agent's reasoning for the live thought stream."""
        entry = {
            "ts": time.time(),
            "agent": agent,
            "thought": thought,
            "category": category,  # observation, decision, warning, learning
        }
        self._thought_stream.append(entry)
        if len(self._thought_stream) > 500:
            self._thought_stream = self._thought_stream[-500:]
        # Publish for dashboard consumption
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.bus.publish("memory.thought", entry))
        except RuntimeError:
            pass  # no event loop — skip broadcast

    def get_thoughts(self, limit: int = 50) -> list[dict]:
        """Get recent thoughts for the dashboard."""
        return self._thought_stream[-limit:]

    # ── Event handlers ─────────────────────────────────────────

    async def _on_report(self, rep: ExecutionReport):
        self.trades.append({
            "intent_id": rep.intent_id,
            "status": rep.status,
            "side": rep.side,
            "asset": rep.asset,
            "qty": rep.quantity,
            "fill_price": rep.fill_price,
            "pnl": rep.pnl_estimate or 0.0,
            "fee": rep.fee_usd,
            "slippage": rep.realized_slippage or 0.0,
            "ts": time.time(),
        })
        # Record thought about trade outcome
        pnl = rep.pnl_estimate or 0.0
        if rep.status == "filled" and abs(pnl) > 0.01:
            emoji = "profit" if pnl > 0 else "loss"
            self.record_thought(
                "Auditor",
                f"Trade {rep.intent_id} {rep.side} {rep.asset}: PnL={pnl:+.4f} ({emoji})",
                "observation",
            )

    async def _on_regime(self, sig):
        try:
            regime = sig.rationale.split("regime=")[1].split()[0]
            if not self.regimes_seen or self.regimes_seen[-1] != regime:
                self.regimes_seen.append(regime)
                self.record_thought(
                    "RegimeDetector",
                    f"Market regime shifted to: {regime}",
                    "observation",
                )
        except (IndexError, ValueError):
            pass

    async def _on_halt(self, payload: dict):
        self.circuit_breaker_events.append({
            "ts": time.time(),
            "reason": payload.get("reason", "unknown"),
        })
        self.record_thought(
            "CircuitBreaker",
            f"HALT triggered: {payload.get('reason', 'unknown')}",
            "warning",
        )

    async def _on_verdict(self, verdict):
        if not verdict.approve:
            self.risk_rejections.append({
                "ts": time.time(),
                "agent": verdict.agent_id,
                "intent": verdict.intent_id,
                "reason": verdict.reason,
            })

    # ── Session summary ────────────────────────────────────────

    def session_summary(self) -> dict:
        """Compile a structured summary of the current session."""
        duration = time.time() - self.start_time
        filled = [t for t in self.trades if t["status"] == "filled"]
        wins = [t for t in filled if t["pnl"] > 0]
        losses = [t for t in filled if t["pnl"] < 0]
        total_pnl = sum(t["pnl"] for t in filled)
        total_fees = sum(t["fee"] for t in filled)
        total_slippage = sum(abs(t["slippage"]) for t in filled)

        # Agent performance: which agents' trades were profitable?
        asset_pnl: dict[str, float] = {}
        for t in filled:
            asset_pnl[t["asset"]] = asset_pnl.get(t["asset"], 0.0) + t["pnl"]

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_s": round(duration, 1),
            "start_equity": round(self.start_equity, 2),
            "end_equity": round(self.portfolio.total_equity(), 2),
            "total_pnl": round(total_pnl, 4),
            "total_fees": round(total_fees, 4),
            "total_slippage": round(total_slippage, 4),
            "trades_total": len(filled),
            "trades_won": len(wins),
            "trades_lost": len(losses),
            "win_rate": round(len(wins) / max(1, len(filled)), 4),
            "avg_win": round(sum(t["pnl"] for t in wins) / max(1, len(wins)), 4),
            "avg_loss": round(sum(t["pnl"] for t in losses) / max(1, len(losses)), 4),
            "regimes_seen": self.regimes_seen,
            "circuit_breaker_events": len(self.circuit_breaker_events),
            "risk_rejections": len(self.risk_rejections),
            "top_rejection_reasons": self._top_rejections(),
            "asset_pnl": asset_pnl,
        }

    def _top_rejections(self, n: int = 5) -> list[dict]:
        """Most common risk rejection reasons."""
        counts: dict[str, int] = {}
        for r in self.risk_rejections:
            key = f"{r['agent']}: {r['reason'][:60]}"
            counts[key] = counts.get(key, 0) + 1
        sorted_items = sorted(counts.items(), key=lambda x: -x[1])
        return [{"reason": k, "count": v} for k, v in sorted_items[:n]]

    # ── Persist on shutdown ────────────────────────────────────

    def write_session_notes(self):
        """Append session lessons to strategy_notes.md and session_log.json."""
        summary = self.session_summary()

        # ── Append to strategy_notes.md ──────────────────────
        note_lines = [
            f"\n## Session — {summary['timestamp']}",
            f"Duration: {summary['duration_s']:.0f}s | "
            f"PnL: {summary['total_pnl']:+.4f} | "
            f"Trades: {summary['trades_total']} "
            f"(W:{summary['trades_won']} L:{summary['trades_lost']} "
            f"WR:{summary['win_rate']:.0%})",
            f"Fees: {summary['total_fees']:.4f} | "
            f"Slippage: {summary['total_slippage']:.4f} | "
            f"Equity: {summary['start_equity']:.2f} -> {summary['end_equity']:.2f}",
        ]

        if summary["regimes_seen"]:
            note_lines.append(f"Regimes: {' -> '.join(summary['regimes_seen'])}")

        if summary["circuit_breaker_events"] > 0:
            note_lines.append(
                f"**Circuit breaker triggered {summary['circuit_breaker_events']} time(s)**"
            )

        if summary["top_rejection_reasons"]:
            note_lines.append("Top risk rejections:")
            for r in summary["top_rejection_reasons"]:
                note_lines.append(f"  - {r['reason']} (x{r['count']})")

        # Derive lessons
        lessons = self._derive_lessons(summary)
        if lessons:
            note_lines.append("### Lessons")
            for lesson in lessons:
                note_lines.append(f"- {lesson}")

        note_lines.append("")

        # Write notes
        existing = self.notes_path.read_text() if self.notes_path.exists() else ""
        if not existing:
            existing = "# Swarm Trade — Strategy Notes\n\n" \
                       "> Auto-generated session notes. The swarm reads these at startup " \
                       "to learn from past sessions.\n"
        self.notes_path.write_text(existing + "\n".join(note_lines))
        log.info("Strategy notes updated: %s", self.notes_path)

        # ── Append to session_log.json ───────────────────────
        history = []
        if self.session_log_path.exists():
            try:
                history = json.loads(self.session_log_path.read_text())
            except (json.JSONDecodeError, KeyError):
                history = []
        history.append(summary)
        # Keep last 100 sessions
        if len(history) > 100:
            history = history[-100:]
        self.session_log_path.write_text(json.dumps(history, indent=2))
        log.info("Session log updated: %s (%d sessions)", self.session_log_path, len(history))

    def _derive_lessons(self, summary: dict) -> list[str]:
        """Auto-generate lessons from session data."""
        lessons = []

        # Win rate assessment
        if summary["trades_total"] >= 3:
            if summary["win_rate"] >= 0.6:
                lessons.append(
                    f"Good session: {summary['win_rate']:.0%} win rate across "
                    f"{summary['trades_total']} trades."
                )
            elif summary["win_rate"] <= 0.3:
                lessons.append(
                    f"Poor win rate ({summary['win_rate']:.0%}). Consider tightening "
                    f"entry thresholds or increasing signal confluence requirements."
                )

        # Fees eating profits
        if summary["total_pnl"] > 0 and summary["total_fees"] > summary["total_pnl"] * 0.5:
            lessons.append(
                "Fees consumed >50% of gross PnL. Consider reducing trade frequency "
                "or using limit orders for better maker rebates."
            )

        # Slippage
        if summary["trades_total"] >= 3 and summary["total_slippage"] > summary["total_fees"]:
            lessons.append(
                "Slippage exceeded fees — consider smaller position sizes or "
                "TWAP execution for large orders."
            )

        # Circuit breaker
        if summary["circuit_breaker_events"] > 0:
            lessons.append(
                f"Circuit breaker fired {summary['circuit_breaker_events']}x. "
                "Review whether entry signals were overriding volatility damping."
            )

        # Regime transitions
        if len(summary["regimes_seen"]) >= 3:
            lessons.append(
                f"Choppy regime environment ({len(summary['regimes_seen'])} transitions). "
                "In unstable regimes, consider widening the strategy threshold."
            )

        # Risk rejections
        if summary["risk_rejections"] > summary["trades_total"] * 3:
            lessons.append(
                f"High rejection rate ({summary['risk_rejections']} rejections vs "
                f"{summary['trades_total']} fills). Risk limits may be too tight, "
                "or signal quality is low."
            )

        return lessons
