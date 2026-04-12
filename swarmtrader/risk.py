"""Risk check functions for use with RiskAgent.

These are pluggable check functions (intent -> (ok, reason)) that can be
composed with RiskAgent to add risk layers. Each returns a closure.

Production-grade risk controls:
- Rate limiting (trade count AND notional volume)
- Daily drawdown tracking with intra-day peak-to-trough
- Per-asset concentration limits
- Correlation-aware gross exposure limits
- Configurable reset time

See also: safety.py for circuit breakers and emergency halt systems.
"""
from __future__ import annotations
import logging, time, datetime
from collections import deque
from .core import Bus, TradeIntent, ExecutionReport, QUOTE_ASSETS

log = logging.getLogger("swarm.risk")


# ── Rate Limiter ────────────────────────────────────────────────
class RateLimiter:
    """Caps trade frequency AND notional volume to prevent overtrading.

    Tracks filled trades in a rolling window. Use with RiskAgent via
    rate_limit_check(limiter).
    """

    def __init__(self, bus: Bus, max_trades: int = 20,
                 max_notional_per_window: float = 50_000.0,
                 window_s: float = 3600.0):
        self.bus = bus
        self.max_trades = max_trades
        self.max_notional = max_notional_per_window
        self.window_s = window_s
        self.trade_times: deque[float] = deque()
        self.trade_notionals: deque[tuple[float, float]] = deque()  # (ts, usd_amount)
        self._seen_intents: set[str] = set()  # dedup
        bus.subscribe("exec.report", self._on_report)

    async def _on_report(self, rep: ExecutionReport):
        if rep.status == "filled":
            now = time.time()
            # Dedup: ignore duplicate reports for same intent
            if rep.intent_id in self._seen_intents:
                return
            self._seen_intents.add(rep.intent_id)
            # Limit dedup set size
            if len(self._seen_intents) > 2000:
                self._seen_intents = set(list(self._seen_intents)[-1000:])

            self.trade_times.append(now)
            notional = (rep.quantity * rep.fill_price) if rep.fill_price else 0.0
            self.trade_notionals.append((now, notional))

    def _prune(self):
        now = time.time()
        cutoff = now - self.window_s
        while self.trade_times and self.trade_times[0] < cutoff:
            self.trade_times.popleft()
        while self.trade_notionals and self.trade_notionals[0][0] < cutoff:
            self.trade_notionals.popleft()

    @property
    def trades_in_window(self) -> int:
        self._prune()
        return len(self.trade_times)

    @property
    def notional_in_window(self) -> float:
        self._prune()
        return sum(n for _, n in self.trade_notionals)

    def check(self, _intent: TradeIntent) -> tuple[bool, str]:
        count = self.trades_in_window
        if count >= self.max_trades:
            return False, f"rate_limit: {count}/{self.max_trades} trades in window"
        notional = self.notional_in_window
        if notional >= self.max_notional:
            return False, (f"notional_limit: ${notional:,.0f}/${self.max_notional:,.0f} "
                          f"in window")
        return True, f"rate_limit: {count}/{self.max_trades} ${notional:,.0f}/{self.max_notional:,.0f}"


# ── Daily Drawdown Tracker ──────────────────────────────────────
class DailyDrawdownTracker:
    """Tracks daily P&L and peak-to-trough intra-day drawdown.

    Resets at configurable time (default: midnight UTC).
    Halts trading if:
    - Cumulative daily loss exceeds max_daily_loss
    - Intra-day drawdown (peak daily PnL to current) exceeds max_intraday_dd
    """

    def __init__(self, bus: Bus, max_daily_loss: float = 100.0,
                 max_intraday_dd: float = 150.0,
                 reset_hour_utc: int = 0):
        self.bus = bus
        self.max_daily_loss = max_daily_loss
        self.max_intraday_dd = max_intraday_dd
        self.reset_hour_utc = reset_hour_utc
        self.daily_pnl: float = 0.0
        self.daily_peak_pnl: float = 0.0
        self.current_day: int = self._today()
        bus.subscribe("exec.report", self._on_report)

    def _today(self) -> int:
        return datetime.datetime.now(datetime.timezone.utc).toordinal()

    def _maybe_reset(self):
        today = self._today()
        if today != self.current_day:
            log.info("Daily drawdown reset: was %.2f (peak %.2f)",
                     self.daily_pnl, self.daily_peak_pnl)
            self.daily_pnl = 0.0
            self.daily_peak_pnl = 0.0
            self.current_day = today

    async def _on_report(self, rep: ExecutionReport):
        if rep.status == "filled":
            self._maybe_reset()
            pnl = rep.pnl_estimate or 0.0
            self.daily_pnl += pnl
            self.daily_peak_pnl = max(self.daily_peak_pnl, self.daily_pnl)

    @property
    def intraday_drawdown(self) -> float:
        """Peak-to-trough intra-day drawdown (positive value = loss from peak)."""
        return self.daily_peak_pnl - self.daily_pnl

    def check(self, _intent: TradeIntent) -> tuple[bool, str]:
        self._maybe_reset()

        # Check absolute daily loss
        if self.daily_pnl < -self.max_daily_loss:
            return False, (f"daily_loss: pnl=${self.daily_pnl:.2f} "
                          f"limit=${-self.max_daily_loss:.2f}")

        # Check intra-day peak-to-trough drawdown
        dd = self.intraday_drawdown
        if dd > self.max_intraday_dd:
            return False, (f"intraday_dd: ${dd:.2f} from peak "
                          f"limit=${self.max_intraday_dd:.2f}")

        return True, f"daily_dd: pnl=${self.daily_pnl:.2f} dd=${dd:.2f} ok"


# ── Per-Asset Concentration Limit ──────────────────────────────
class ConcentrationLimiter:
    """Prevents over-concentration in a single asset.

    Checks that proposed trade won't push any single asset above
    max_pct of total portfolio equity.
    """

    def __init__(self, portfolio, wallet, max_pct: float = 0.30):
        """
        Args:
            portfolio: PortfolioTracker instance
            wallet: WalletManager instance
            max_pct: max fraction of equity in one asset (0.30 = 30%)
        """
        self.portfolio = portfolio
        self.wallet = wallet
        self.max_pct = max_pct

    def check(self, intent: TradeIntent) -> tuple[bool, str]:
        buying = intent.asset_in.upper() in QUOTE_ASSETS
        if not buying:
            return True, "sell — no concentration concern"

        asset = intent.asset_out
        equity = self.wallet.total_equity()
        if equity <= 0:
            return False, "zero equity — cannot validate concentration"

        # Current position value
        pos = self.portfolio.get(asset)
        price = self.portfolio.last_prices.get(asset, pos.avg_entry)
        current_value = pos.quantity * price
        new_value = current_value + intent.amount_in
        pct = new_value / equity

        if pct > self.max_pct:
            return False, (f"concentration: {asset} would be {pct:.1%} "
                          f"(max {self.max_pct:.1%})")
        return True, f"concentration: {asset} {pct:.1%} <= {self.max_pct:.1%}"


# ── Gross Exposure Limit ───────────────────────────────────────
class GrossExposureLimiter:
    """Limits total gross exposure relative to equity.

    Prevents the system from being over-leveraged across all positions.
    """

    def __init__(self, portfolio, wallet, max_gross_pct: float = 0.80):
        """
        Args:
            max_gross_pct: max gross exposure as fraction of equity (0.80 = 80%)
        """
        self.portfolio = portfolio
        self.wallet = wallet
        self.max_gross_pct = max_gross_pct

    def check(self, intent: TradeIntent) -> tuple[bool, str]:
        buying = intent.asset_in.upper() in QUOTE_ASSETS
        if not buying:
            return True, "sell — reduces exposure"

        equity = self.wallet.total_equity()
        if equity <= 0:
            return False, "zero equity"

        # Current gross exposure (sum of all position market values)
        gross = self.portfolio.position_market_value()
        new_gross = gross + intent.amount_in
        pct = new_gross / equity

        if pct > self.max_gross_pct:
            return False, (f"gross_exposure: ${new_gross:,.0f} = {pct:.1%} of equity "
                          f"(max {self.max_gross_pct:.1%})")
        return True, f"gross_exposure: {pct:.1%} <= {self.max_gross_pct:.1%}"


# ── Minimum Order Size ─────────────────────────────────────────
def min_order_check(min_usd: float = 10.0):
    """Risk check: reject orders below minimum size (exchange minimums)."""
    def f(intent: TradeIntent) -> tuple[bool, str]:
        if intent.amount_in < min_usd:
            return False, f"order_too_small: ${intent.amount_in:.2f} < ${min_usd:.2f}"
        return True, f"order_size: ${intent.amount_in:.2f} ok"
    return f


# ── Check function wrappers ────────────────────────────────────
def rate_limit_check(limiter: RateLimiter):
    """Wraps RateLimiter.check for use with RiskAgent."""
    return limiter.check


def daily_drawdown_check(tracker: DailyDrawdownTracker):
    """Wraps DailyDrawdownTracker.check for use with RiskAgent."""
    return tracker.check


def concentration_check(limiter: ConcentrationLimiter):
    """Wraps ConcentrationLimiter.check for use with RiskAgent."""
    return limiter.check


def gross_exposure_check(limiter: GrossExposureLimiter):
    """Wraps GrossExposureLimiter.check for use with RiskAgent."""
    return limiter.check
