"""Exchange balance reconciliation — detects drift between local ledger and exchange.

Phase 4: Periodically compares PortfolioTracker's view of balances against
the actual exchange balances via Kraken CLI. Alerts on discrepancies.

Usage:
    recon = BalanceReconciler(bus, portfolio, wallet, interval=60.0)
    supervisor.register("reconciler", recon.run, stale_after=120.0)
"""
from __future__ import annotations
import asyncio, logging, time
from .core import Bus, PortfolioTracker

log = logging.getLogger("swarm.recon")

# Maximum acceptable drift between local and exchange balance (absolute USD)
_DEFAULT_MAX_DRIFT_USD = 5.0
# Maximum acceptable drift as percentage of position value
_DEFAULT_MAX_DRIFT_PCT = 0.02  # 2%


class BalanceReconciler:
    """Periodically reconciles local position tracking with exchange balances.

    Compares:
    - Local PortfolioTracker quantities vs Kraken balance API
    - Local cash balance vs Kraken USD balance
    - Detects missing fills (exchange has position we don't know about)
    - Detects phantom positions (we think we hold something, exchange disagrees)

    Publishes:
    - recon.ok: reconciliation passed
    - recon.drift: drift detected (with details)
    - recon.error: reconciliation failed (API error, etc.)
    """

    def __init__(self, bus: Bus, portfolio: PortfolioTracker,
                 wallet=None,
                 interval: float = 60.0,
                 max_drift_usd: float = _DEFAULT_MAX_DRIFT_USD,
                 max_drift_pct: float = _DEFAULT_MAX_DRIFT_PCT):
        self.bus = bus
        self.portfolio = portfolio
        self.wallet = wallet
        self.interval = interval
        self.max_drift_usd = max_drift_usd
        self.max_drift_pct = max_drift_pct
        self._stop = False
        self._last_recon_ts: float = 0.0
        self._consecutive_failures: int = 0
        self._drift_history: list[dict] = []

    def stop(self):
        self._stop = True

    async def run(self):
        """Main reconciliation loop."""
        log.info("BalanceReconciler started (interval=%.0fs, max_drift=$%.2f / %.1f%%)",
                 self.interval, self.max_drift_usd, self.max_drift_pct * 100)
        while not self._stop:
            try:
                await self._reconcile()
                self._consecutive_failures = 0
            except Exception as e:
                self._consecutive_failures += 1
                log.warning("Reconciliation failed (attempt %d): %s",
                            self._consecutive_failures, e)
                await self.bus.publish("recon.error", {
                    "error": str(e),
                    "consecutive_failures": self._consecutive_failures,
                })
            await asyncio.sleep(self.interval)

    async def _reconcile(self):
        """Perform one reconciliation cycle."""
        from .kraken import _run_cli

        # Fetch exchange balances
        exchange_balances = await _run_cli("balance")
        if not isinstance(exchange_balances, dict):
            raise RuntimeError(f"Unexpected balance response: {exchange_balances}")

        # Normalize exchange balance keys (XETH -> ETH, XXBT -> BTC, ZUSD -> USD)
        normalized: dict[str, float] = {}
        for key, val in exchange_balances.items():
            try:
                symbol = self._normalize_asset(key)
                amount = float(val)
            except (ValueError, TypeError) as e:
                log.warning("Skipping malformed balance entry %s=%s: %s", key, val, e)
                continue
            if amount > 1e-9:
                normalized[symbol] = amount

        # Compare with local portfolio
        drifts: list[dict] = []
        checked_assets: set[str] = set()

        # Check all local positions against exchange
        for asset, pos in self.portfolio.positions.items():
            if pos.quantity < 1e-9:
                continue
            checked_assets.add(asset)

            exchange_qty = normalized.get(asset, 0.0)
            local_qty = pos.quantity
            diff = abs(exchange_qty - local_qty)

            # Check absolute and percentage drift
            price = self.portfolio.last_prices.get(asset, pos.avg_entry)
            diff_usd = diff * price
            diff_pct = diff / max(local_qty, 1e-9)

            if diff_usd > self.max_drift_usd or diff_pct > self.max_drift_pct:
                drift = {
                    "asset": asset,
                    "local_qty": round(local_qty, 6),
                    "exchange_qty": round(exchange_qty, 6),
                    "diff_qty": round(exchange_qty - local_qty, 6),
                    "diff_usd": round(diff_usd, 2),
                    "diff_pct": round(diff_pct * 100, 2),
                    "ts": time.time(),
                }
                drifts.append(drift)
                log.warning("RECON DRIFT: %s local=%.6f exchange=%.6f diff=$%.2f (%.1f%%)",
                            asset, local_qty, exchange_qty, diff_usd, diff_pct * 100)

        # Check for assets on exchange that we don't track locally
        for asset, exchange_qty in normalized.items():
            if asset in checked_assets or asset in ("USD", "USDC", "USDT"):
                continue
            if exchange_qty > 1e-6:
                price = self.portfolio.last_prices.get(asset, 0.0)
                value = exchange_qty * price
                if value > self.max_drift_usd:
                    drifts.append({
                        "asset": asset,
                        "local_qty": 0.0,
                        "exchange_qty": round(exchange_qty, 6),
                        "diff_qty": round(exchange_qty, 6),
                        "diff_usd": round(value, 2),
                        "diff_pct": 100.0,
                        "ts": time.time(),
                        "note": "untracked position on exchange",
                    })
                    log.warning("RECON UNTRACKED: %s has %.6f on exchange (not in local ledger)",
                                asset, exchange_qty)

        # Check cash balance if wallet available
        if self.wallet:
            exchange_usd = normalized.get("USD", 0.0)
            local_usd = self.wallet.cash_balance
            cash_diff = abs(exchange_usd - local_usd)
            if cash_diff > self.max_drift_usd:
                drifts.append({
                    "asset": "USD",
                    "local_qty": round(local_usd, 2),
                    "exchange_qty": round(exchange_usd, 2),
                    "diff_qty": round(exchange_usd - local_usd, 2),
                    "diff_usd": round(cash_diff, 2),
                    "diff_pct": round(cash_diff / max(local_usd, 1) * 100, 2),
                    "ts": time.time(),
                })
                log.warning("RECON CASH DRIFT: local=$%.2f exchange=$%.2f diff=$%.2f",
                            local_usd, exchange_usd, cash_diff)

        self._last_recon_ts = time.time()
        self._drift_history.extend(drifts)
        # Keep last 100 drift records
        if len(self._drift_history) > 100:
            self._drift_history = self._drift_history[-100:]

        if drifts:
            await self.bus.publish("recon.drift", {
                "drifts": drifts,
                "ts": time.time(),
            })
        else:
            await self.bus.publish("recon.ok", {
                "assets_checked": len(checked_assets),
                "ts": time.time(),
            })
            log.debug("Reconciliation OK: %d assets checked", len(checked_assets))

    @staticmethod
    def _normalize_asset(kraken_key: str) -> str:
        """Convert Kraken balance keys to our internal names."""
        k = kraken_key.upper()
        renames = {
            "XXBT": "BTC", "XBT": "BTC",
            "XETH": "ETH", "XLTC": "LTC", "XXRP": "XRP",
            "ZUSD": "USD", "USDT": "USDT",
        }
        if k in renames:
            return renames[k]
        # Strip leading X/Z for Kraken legacy names
        if len(k) == 4 and k[0] in ("X", "Z"):
            return k[1:]
        return k

    def summary(self) -> dict:
        return {
            "last_recon_ts": self._last_recon_ts,
            "consecutive_failures": self._consecutive_failures,
            "recent_drifts": self._drift_history[-10:],
        }
