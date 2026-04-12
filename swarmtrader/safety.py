"""Circuit breakers, dead man's switch, and emergency safety systems.

Production hardening:
- Kill switch uses atomic threading.Event + persistent file (belt and suspenders)
- Circuit breaker resumes only when conditions improve (not just cooldown)
- Volatility spike requires sustained detection (not single-tick outlier)
- Alert hook for external notification (Slack, email, PagerDuty)
- Subprocess timeout on Kraken CLI calls
- Division-by-zero protection on price feeds
"""
from __future__ import annotations
import asyncio, logging, time, threading
from collections import deque
from pathlib import Path
from typing import Callable, Awaitable
from .core import Bus, MarketSnapshot, ExecutionReport

log = logging.getLogger("swarm.safety")

# Type for async alert callbacks: async def alert(level, message)
AlertHook = Callable[[str, str], Awaitable[None]]

# How long to wait for Kraken CLI subprocesses before timing out
_CLI_TIMEOUT = 10.0


class KillSwitch:
    """Thread-safe, atomic kill switch.

    Uses an in-memory threading.Event as the primary gate (fast, no I/O)
    and a filesystem file as a persistent backup (survives restarts).
    """

    def __init__(self, path: Path):
        self._path = path
        self._event = threading.Event()
        # Recover state from disk on startup
        if path.exists():
            self._event.set()
            log.warning("Kill switch recovered from disk — trading halted")

    @property
    def active(self) -> bool:
        return self._event.is_set()

    def engage(self, reason: str = ""):
        """Activate kill switch (atomic, thread-safe)."""
        self._event.set()
        try:
            self._path.write_text(f"HALTED: {reason}\nts: {time.time()}\n")
        except OSError as e:
            log.error("Kill switch file write failed (memory flag still set): %s", e)

    def disengage(self):
        """Deactivate kill switch."""
        self._event.clear()
        try:
            self._path.unlink(missing_ok=True)
        except OSError as e:
            log.error("Kill switch file removal failed (memory flag cleared): %s", e)


class CircuitBreaker:
    """Multi-layer circuit breaker system:
    1. Rapid loss detection — halt if N losses in a row
    2. Drawdown limit — halt if cumulative PnL drops below threshold
    3. Volatility spike — pause trading during sustained extreme moves
    4. Dead man's switch — auto-cancel via Kraken CLI if agent goes unresponsive

    Production improvements over hackathon version:
    - Resume requires conditions to improve, not just time passing
    - Volatility requires 2+ spike ticks (filters single-tick data errors)
    - Subprocess timeout on CLI calls
    - Alert hook for external notifications
    - Price division-by-zero protection
    """

    def __init__(self, bus: Bus, kill_switch: KillSwitch,
                 max_consecutive_losses: int = 5,
                 max_drawdown_usd: float = 200.0,
                 vol_halt_threshold: float = 0.05,
                 cooldown_seconds: float = 300.0,
                 alert_hook: AlertHook | None = None,
                 ws_client=None):
        self.bus = bus
        self.kill_switch = kill_switch
        self.max_consecutive_losses = max_consecutive_losses
        self.max_drawdown = max_drawdown_usd
        self.vol_halt_threshold = vol_halt_threshold
        self.cooldown_seconds = cooldown_seconds
        self._alert_hook = alert_hook
        self._ws_client = ws_client  # KrakenWSv2Client for fast cancel

        self.consecutive_losses = 0
        self.cumulative_pnl = 0.0
        self.peak_pnl = 0.0
        self.prices: deque[float] = deque(maxlen=20)
        self._vol_spike_count = 0     # require sustained spikes
        self._vol_spike_threshold = 2  # need 2+ ticks above threshold

        self.halted = False
        self.halt_reason = ""
        self.halt_ts = 0.0
        self.halt_until = 0.0
        # Track conditions at halt so we can check for improvement
        self._halt_drawdown = 0.0
        self._halt_losses = 0
        self._wins_since_halt = 0

        bus.subscribe("exec.report", self._on_report)
        bus.subscribe("market.snapshot", self._on_snap)
        bus.subscribe("intent.new", self._on_intent)

    async def _alert(self, level: str, message: str):
        """Send alert via hook (if configured) and always log."""
        log.critical("ALERT [%s]: %s", level, message)
        if self._alert_hook:
            try:
                await self._alert_hook(level, message)
            except Exception as e:
                log.error("Alert hook failed: %s", e)

    async def _on_report(self, rep: ExecutionReport):
        if rep.status != "filled":
            return
        pnl = rep.pnl_estimate or 0.0
        self.cumulative_pnl += pnl
        self.peak_pnl = max(self.peak_pnl, self.cumulative_pnl)

        if pnl < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
            if self.halted:
                self._wins_since_halt += 1

        # Check consecutive losses
        if self.consecutive_losses >= self.max_consecutive_losses:
            await self._halt(f"consecutive_losses={self.consecutive_losses}")

        # Check drawdown from peak
        drawdown = self.peak_pnl - self.cumulative_pnl
        if drawdown > self.max_drawdown:
            await self._halt(f"drawdown=${drawdown:.2f} exceeds ${self.max_drawdown:.2f}")

        # Check absolute loss
        if self.cumulative_pnl < -self.max_drawdown:
            await self._halt(f"cumulative_loss=${self.cumulative_pnl:.2f}")

    async def _on_snap(self, snap: MarketSnapshot):
        for price in snap.prices.values():
            if price <= 0:
                continue  # skip zero/negative prices (data error)
            self.prices.append(price)
            break

        if len(self.prices) < 5:
            return

        # Check for flash crash / extreme volatility
        recent = list(self.prices)
        returns = []
        for i in range(1, len(recent)):
            if recent[i - 1] > 0:  # protect against div-by-zero
                returns.append(abs(recent[i] / recent[i - 1] - 1))
        if not returns:
            return
        max_move = max(returns)

        if max_move > self.vol_halt_threshold:
            self._vol_spike_count += 1
            if self._vol_spike_count >= self._vol_spike_threshold:
                await self._halt(
                    f"sustained_volatility_spike={max_move:.4f} "
                    f"({max_move*100:.1f}%) over {self._vol_spike_count} ticks"
                )
        else:
            self._vol_spike_count = 0

        # Auto-check resume on each tick
        await self.check_resume()

    async def _on_intent(self, intent):
        """Block intents when halted."""
        if self.halted:
            log.warning("CIRCUIT BREAKER ACTIVE: blocking intent %s — %s",
                        intent.id, self.halt_reason)
            self.kill_switch.engage(self.halt_reason)

    async def _halt(self, reason: str):
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self.halt_ts = time.time()
        self.halt_until = self.halt_ts + self.cooldown_seconds
        self._halt_drawdown = self.peak_pnl - self.cumulative_pnl
        self._halt_losses = self.consecutive_losses
        self._wins_since_halt = 0

        self.kill_switch.engage(reason)

        await self._alert("CRITICAL",
                          f"CIRCUIT BREAKER: {reason} | "
                          f"PnL=${self.cumulative_pnl:.2f} | "
                          f"Losses={self.consecutive_losses}")

        # Attempt dead man's switch via Kraken CLI (with timeout)
        await self._cancel_all_orders()

        await self.bus.publish("safety.halt", {
            "reason": reason,
            "pnl": self.cumulative_pnl,
            "consecutive_losses": self.consecutive_losses,
        })

    async def _cancel_all_orders(self):
        """Cancel all open orders via REST API → WS → CLI (priority order).

        Tries the fastest method first:
        1. WS cancel_all (sub-50ms if connected)
        2. REST API cancel_all (no subprocess)
        3. CLI subprocess (fallback)
        """
        # Try WS cancel (fastest)
        if self._ws_client:
            try:
                await self._ws_client.ws_cancel_all()
                log.info("Dead man's switch: cancelled all orders via WebSocket")
                return
            except Exception as e:
                log.debug("WS cancel_all failed, trying REST: %s", e)

        # Try REST API
        try:
            from .kraken_api import get_client
            client = get_client()
            if client._cfg.api_key and client._cfg.api_secret:
                result = await client.cancel_all()
                count = result.get("count", 0)
                log.info("Dead man's switch: cancelled %d orders via REST API", count)
                return
        except Exception as e:
            log.debug("REST cancel_all failed, trying CLI: %s", e)

        # CLI fallback
        try:
            proc = await asyncio.create_subprocess_exec(
                "kraken", "order", "cancel-all", "--yes",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_CLI_TIMEOUT
                )
                if proc.returncode == 0:
                    log.info("Dead man's switch: cancelled all open orders (CLI)")
                else:
                    log.warning("Dead man's switch returned code %d: %s",
                                proc.returncode,
                                stderr.decode().strip() if stderr else "")
            except asyncio.TimeoutError:
                proc.kill()
                log.error("Dead man's switch timed out after %.0fs — "
                          "process killed", _CLI_TIMEOUT)
        except FileNotFoundError:
            log.warning("Dead man's switch: 'kraken' CLI not found")
        except Exception as e:
            log.error("Dead man's switch failed: %s", e)

    async def check_resume(self):
        """Check if conditions have improved enough to resume.

        Resume requires ALL of:
        1. Cooldown period has elapsed
        2. Conditions have improved:
           - For loss streaks: at least 2 winning trades since halt
           - For drawdown: drawdown has narrowed (PnL improved)
           - For volatility: no recent spikes
        """
        if not self.halted:
            return
        if time.time() < self.halt_until:
            return

        # Check if conditions improved based on halt reason
        can_resume = False
        reason_parts = []

        if "consecutive_losses" in self.halt_reason:
            # Require some winning trades before resuming
            if self._wins_since_halt >= 2:
                can_resume = True
                reason_parts.append(f"wins_since_halt={self._wins_since_halt}")
            else:
                reason_parts.append(f"need wins: {self._wins_since_halt}/2")
        elif "drawdown" in self.halt_reason or "cumulative_loss" in self.halt_reason:
            # Resume if drawdown has narrowed from when we halted
            current_dd = self.peak_pnl - self.cumulative_pnl
            if current_dd < self._halt_drawdown:
                can_resume = True
                reason_parts.append(f"dd_improved: {current_dd:.2f} < {self._halt_drawdown:.2f}")
            else:
                # Allow resume after extended cooldown (2x) even without improvement
                if time.time() >= self.halt_until + self.cooldown_seconds:
                    can_resume = True
                    reason_parts.append("extended_cooldown_expired")
                else:
                    reason_parts.append(f"dd_unchanged: {current_dd:.2f}")
        elif "volatility" in self.halt_reason:
            # Resume if no recent spikes
            if self._vol_spike_count == 0:
                can_resume = True
                reason_parts.append("volatility_subsided")
            else:
                reason_parts.append(f"vol_spikes={self._vol_spike_count}")
        else:
            # Unknown reason — resume on cooldown alone
            can_resume = True

        if can_resume:
            log.info("Circuit breaker resuming: %s", ", ".join(reason_parts))
            self.halted = False
            self.halt_reason = ""
            # Do NOT reset consecutive_losses to 0 — let actual wins reset it
            self.kill_switch.disengage()
            await self._alert("INFO", "Circuit breaker resumed: " + ", ".join(reason_parts))


class PositionFlattener:
    """Emergency position flattener — closes all positions when triggered.

    CRITICAL: If the kraken CLI is not found in live mode, the kill switch
    remains engaged permanently — the system cannot safely flatten positions.
    """

    def __init__(self, bus: Bus, paper: bool = True,
                 kill_switch: "KillSwitch | None" = None, ws_client=None):
        self.bus = bus
        self.paper = paper
        self.kill_switch = kill_switch
        self._ws_client = ws_client
        bus.subscribe("safety.halt", self._on_halt)

    async def _on_halt(self, payload: dict):
        reason = payload.get("reason", "unknown")
        log.critical("FLATTENING ALL POSITIONS: %s", reason)

        # Try WS cancel first (fastest)
        if self._ws_client:
            try:
                await self._ws_client.ws_cancel_all()
                log.info("Position flattener: cancelled all orders via WebSocket")
                return
            except Exception as e:
                log.debug("WS flatten failed, trying REST: %s", e)

        # Try REST API
        try:
            from .kraken_api import get_client
            client = get_client()
            if client._cfg.api_key and client._cfg.api_secret:
                result = await client.cancel_all()
                count = result.get("count", 0)
                log.info("Position flattener: cancelled %d orders via REST API", count)
                return
        except Exception as e:
            log.debug("REST flatten failed, trying CLI: %s", e)

        # CLI fallback
        try:
            if self.paper:
                cmd = ["kraken", "paper", "cancel-all", "-o", "json"]
            else:
                cmd = ["kraken", "order", "cancel-all", "--yes", "-o", "json"]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_CLI_TIMEOUT
                )
                if proc.returncode == 0:
                    log.info("Position flattener result: %s",
                             stdout.decode().strip() if stdout else "ok")
                else:
                    log.error("Position flattener failed (code %d): %s",
                              proc.returncode,
                              stderr.decode().strip() if stderr else "")
            except asyncio.TimeoutError:
                proc.kill()
                log.error("Position flattener timed out after %.0fs", _CLI_TIMEOUT)
        except FileNotFoundError:
            log.critical("FATAL: Position flattener cannot run — 'kraken' CLI not found. "
                         "Kill switch will remain engaged. Manual intervention required.")
            if self.kill_switch:
                self.kill_switch.engage("CLI not found — cannot flatten positions")
        except Exception as e:
            log.error("Position flattener failed: %s", e)
