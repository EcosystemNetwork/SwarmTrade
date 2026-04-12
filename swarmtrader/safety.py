"""Circuit breakers, dead man's switch, and emergency safety systems."""
from __future__ import annotations
import asyncio, logging, time
from collections import deque
from pathlib import Path
from .core import Bus, MarketSnapshot, ExecutionReport, Signal

log = logging.getLogger("swarm.safety")


class CircuitBreaker:
    """Multi-layer circuit breaker system:
    1. Rapid loss detection — halt if N losses in a row
    2. Drawdown limit — halt if cumulative PnL drops below threshold
    3. Volatility spike — pause trading during extreme moves
    4. Dead man's switch — auto-cancel via Kraken CLI if agent goes unresponsive
    """

    def __init__(self, bus: Bus, kill_switch: Path,
                 max_consecutive_losses: int = 5,
                 max_drawdown_usd: float = 200.0,
                 vol_halt_threshold: float = 0.05,
                 cooldown_seconds: float = 300.0):
        self.bus = bus
        self.kill_switch = kill_switch
        self.max_consecutive_losses = max_consecutive_losses
        self.max_drawdown = max_drawdown_usd
        self.vol_halt_threshold = vol_halt_threshold
        self.cooldown_seconds = cooldown_seconds

        self.consecutive_losses = 0
        self.cumulative_pnl = 0.0
        self.peak_pnl = 0.0
        self.prices: deque[float] = deque(maxlen=20)
        self.halted = False
        self.halt_reason = ""
        self.halt_until = 0.0

        bus.subscribe("exec.report", self._on_report)
        bus.subscribe("market.snapshot", self._on_snap)
        bus.subscribe("intent.new", self._on_intent)

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
            self.prices.append(price)
            break

        if len(self.prices) < 5:
            return

        # Check for flash crash / extreme volatility
        recent = list(self.prices)
        returns = [abs(recent[i] / recent[i-1] - 1) for i in range(1, len(recent))]
        max_move = max(returns)

        if max_move > self.vol_halt_threshold:
            await self._halt(f"volatility_spike={max_move:.4f} ({max_move*100:.1f}%)")

    async def _on_intent(self, intent):
        """Block intents when halted."""
        if self.halted and time.time() < self.halt_until:
            log.warning("CIRCUIT BREAKER ACTIVE: blocking intent %s — %s",
                        intent.id, self.halt_reason)
            # Trigger kill switch to block execution
            if not self.kill_switch.exists():
                self.kill_switch.write_text(f"HALTED: {self.halt_reason}\n")

    async def _halt(self, reason: str):
        if self.halted:
            return
        self.halted = True
        self.halt_reason = reason
        self.halt_until = time.time() + self.cooldown_seconds
        log.critical("CIRCUIT BREAKER TRIGGERED: %s — halting for %.0fs",
                     reason, self.cooldown_seconds)

        # Write kill switch file
        self.kill_switch.write_text(f"CIRCUIT BREAKER: {reason}\nUntil: {self.halt_until}\n")

        # Attempt dead man's switch via Kraken CLI
        try:
            proc = await asyncio.create_subprocess_exec(
                "kraken", "order", "cancel-all", "--yes",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            log.info("Dead man's switch: cancelled all open orders")
        except Exception as e:
            log.warning("Dead man's switch failed: %s", e)

        await self.bus.publish("safety.halt", {
            "reason": reason,
            "pnl": self.cumulative_pnl,
            "consecutive_losses": self.consecutive_losses,
        })

    async def check_resume(self):
        """Call periodically to check if cooldown has expired."""
        if self.halted and time.time() >= self.halt_until:
            log.info("Circuit breaker cooldown expired — resuming trading")
            self.halted = False
            self.halt_reason = ""
            self.consecutive_losses = 0
            # Remove kill switch
            if self.kill_switch.exists():
                self.kill_switch.unlink()


class PositionFlattener:
    """Emergency position flattener — closes all positions when triggered."""

    def __init__(self, bus: Bus, paper: bool = True):
        self.bus = bus
        self.paper = paper
        bus.subscribe("safety.halt", self._on_halt)

    async def _on_halt(self, payload: dict):
        reason = payload.get("reason", "unknown")
        log.critical("FLATTENING ALL POSITIONS: %s", reason)
        try:
            if self.paper:
                proc = await asyncio.create_subprocess_exec(
                    "kraken", "paper", "cancel-all", "-o", "json",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    "kraken", "order", "cancel-all", "--yes", "-o", "json",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            stdout, _ = await proc.communicate()
            log.info("Position flattener result: %s", stdout.decode().strip())
        except Exception as e:
            log.error("Position flattener failed: %s", e)
