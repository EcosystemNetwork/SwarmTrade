"""Risk check functions for use with RiskAgent.

These are pluggable check functions (intent -> (ok, reason)) that can be
composed with RiskAgent to add risk layers. Each returns a closure.

Rate limiting and daily drawdown tracking are stateful — they subscribe
to the bus to track execution reports automatically.

See also: safety.py for circuit breakers and emergency halt systems.
"""
from __future__ import annotations
import logging, time
from collections import deque
from .core import Bus, TradeIntent, ExecutionReport

log = logging.getLogger("swarm.risk")


# ── Rate Limiter ────────────────────────────────────────────────
class RateLimiter:
    """Caps trade frequency to prevent overtrading in choppy markets.

    Tracks filled trades in a rolling window. Use with RiskAgent via
    rate_limit_check(limiter).
    """

    def __init__(self, bus: Bus, max_trades: int = 20, window_s: float = 3600.0):
        self.bus = bus
        self.max_trades = max_trades
        self.window_s = window_s
        self.trade_times: deque[float] = deque()
        bus.subscribe("exec.report", self._on_report)

    async def _on_report(self, rep: ExecutionReport):
        if rep.status == "filled":
            self.trade_times.append(time.time())

    @property
    def trades_in_window(self) -> int:
        now = time.time()
        cutoff = now - self.window_s
        while self.trade_times and self.trade_times[0] < cutoff:
            self.trade_times.popleft()
        return len(self.trade_times)

    def check(self, _intent: TradeIntent) -> tuple[bool, str]:
        count = self.trades_in_window
        if count >= self.max_trades:
            return False, f"rate_limit: {count}/{self.max_trades} trades in window"
        return True, f"rate_limit: {count}/{self.max_trades}"


# ── Daily Drawdown Tracker ──────────────────────────────────────
class DailyDrawdownTracker:
    """Resets drawdown tracking at midnight UTC automatically."""

    def __init__(self, bus: Bus, max_daily_loss: float = 100.0):
        self.bus = bus
        self.max_daily_loss = max_daily_loss
        self.daily_pnl: float = 0.0
        self.current_day: int = self._today()
        bus.subscribe("exec.report", self._on_report)

    @staticmethod
    def _today() -> int:
        import datetime
        return datetime.datetime.utcnow().toordinal()

    def _maybe_reset(self):
        today = self._today()
        if today != self.current_day:
            log.info("Daily drawdown reset: was %.2f", self.daily_pnl)
            self.daily_pnl = 0.0
            self.current_day = today

    async def _on_report(self, rep: ExecutionReport):
        if rep.status == "filled":
            self._maybe_reset()
            self.daily_pnl += rep.pnl_estimate or 0.0

    def check(self, _intent: TradeIntent) -> tuple[bool, str]:
        self._maybe_reset()
        if self.daily_pnl < -self.max_daily_loss:
            return False, f"daily_dd: pnl={self.daily_pnl:.2f} limit={-self.max_daily_loss:.2f}"
        return True, f"daily_dd: pnl={self.daily_pnl:.2f} ok"


# ── Check function wrappers ────────────────────────────────────
def rate_limit_check(limiter: RateLimiter):
    """Wraps RateLimiter.check for use with RiskAgent."""
    return limiter.check
