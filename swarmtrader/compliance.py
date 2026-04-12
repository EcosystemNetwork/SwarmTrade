"""Pre-trade compliance checks and infrastructure monitors.

Provides wash-trade detection, position limits, margin monitoring,
internal/exchange reconciliation, and data quality monitoring.
All components integrate via the async pub/sub Bus.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field

from .core import (
    Bus,
    ExecutionReport,
    MarketSnapshot,
    PortfolioTracker,
    TradeIntent,
    QUOTE_ASSETS,
)

log = logging.getLogger("swarm.compliance")


# ── Helpers ────────────────────────────────────────────────────────

def _base_asset(intent: TradeIntent) -> str:
    """Extract the base (non-quote) asset from an intent."""
    if intent.asset_in.upper() in QUOTE_ASSETS:
        return intent.asset_out
    return intent.asset_in


def _is_buy(intent: TradeIntent) -> bool:
    return intent.asset_in.upper() in QUOTE_ASSETS


# ── Wash Trading Detector ──────────────────────────────────────────

class WashTradingDetector:
    """Detects wash trading patterns — rapid buy/sell (or sell/buy)
    of the same asset with minimal net position change.

    Subscribes to ``exec.report`` and publishes ``compliance.wash_warning``
    when a wash pattern is detected.
    """

    def __init__(
        self,
        bus: Bus,
        wash_window: float = 30.0,
        net_threshold: float = 0.01,
        history_size: int = 500,
    ):
        self.bus = bus
        self.wash_window = wash_window
        self.net_threshold = net_threshold
        self.history_size = history_size
        # {asset: deque[(ts, side, qty, price)]}
        self.recent_trades: dict[str, deque[tuple[float, str, float, float]]] = {}
        bus.subscribe("exec.report", self._on_report)

    async def _on_report(self, rep: ExecutionReport) -> None:
        if rep.status != "filled" or not rep.asset:
            return
        asset = rep.asset
        if asset not in self.recent_trades:
            self.recent_trades[asset] = deque(maxlen=self.history_size)
        self.recent_trades[asset].append(
            (time.time(), rep.side, rep.quantity, rep.fill_price or 0.0)
        )

    def _prune(self, asset: str) -> None:
        """Remove trades older than wash_window."""
        cutoff = time.time() - self.wash_window
        dq = self.recent_trades.get(asset)
        if dq is None:
            return
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def is_wash(self, intent: TradeIntent) -> tuple[bool, str]:
        """Check whether executing *intent* would create a wash trade.

        Returns ``(True, reason)`` if wash detected, ``(False, "ok")`` otherwise.
        """
        asset = _base_asset(intent)
        self._prune(asset)
        trades = self.recent_trades.get(asset)
        if not trades:
            return False, "ok"

        proposed_side = "buy" if _is_buy(intent) else "sell"
        proposed_qty = (
            intent.amount_in / (trades[-1][3] or 1.0)
            if proposed_side == "buy" and trades[-1][3]
            else intent.amount_in
        )

        # Look for opposite-side trades in the window
        for ts, side, qty, price in trades:
            if side != proposed_side:
                # Net position change would be small
                net_change = abs(qty - proposed_qty)
                relative = net_change / max(qty, proposed_qty, 1e-9)
                if relative < self.net_threshold:
                    reason = (
                        f"wash: {side} {qty:.6f} then {proposed_side} "
                        f"{proposed_qty:.6f} {asset} within {self.wash_window}s "
                        f"(net_change_pct={relative:.4f})"
                    )
                    log.warning(reason)
                    return True, reason

        return False, "ok"

    async def check_and_publish(self, intent: TradeIntent) -> tuple[bool, str]:
        """Check for wash and publish warning if detected."""
        is_w, reason = self.is_wash(intent)
        if is_w:
            await self.bus.publish(
                "compliance.wash_warning",
                {"intent_id": intent.id, "asset": _base_asset(intent), "reason": reason},
            )
        return is_w, reason


# ── Position Limit Checker ─────────────────────────────────────────

class PositionLimitChecker:
    """Enforces per-asset and total portfolio position limits.

    Tracks gross and net exposure from a ``PortfolioTracker``.
    """

    def __init__(
        self,
        portfolio: PortfolioTracker,
        per_asset_limit: float = 10_000.0,
        total_limit: float = 50_000.0,
        per_asset_overrides: dict[str, float] | None = None,
    ):
        self.portfolio = portfolio
        self.per_asset_limit = per_asset_limit
        self.total_limit = total_limit
        self.per_asset_overrides: dict[str, float] = per_asset_overrides or {}

    def gross_exposure(self) -> float:
        """Total absolute USD value across all positions."""
        total = 0.0
        for asset, pos in self.portfolio.positions.items():
            price = self.portfolio.last_prices.get(asset, pos.avg_entry)
            total += pos.quantity * price
        return total

    def net_exposure(self) -> float:
        """Net USD value (same as gross for long-only spot)."""
        return self.gross_exposure()

    def asset_exposure(self, asset: str) -> float:
        pos = self.portfolio.get(asset)
        price = self.portfolio.last_prices.get(asset, pos.avg_entry)
        return pos.quantity * price

    def check(self, intent: TradeIntent) -> tuple[bool, str]:
        """Reject if position limits would be breached after this trade."""
        if not _is_buy(intent):
            return True, "sell: limits n/a"

        asset = _base_asset(intent)
        new_exposure = self.asset_exposure(asset) + intent.amount_in
        limit = self.per_asset_overrides.get(asset, self.per_asset_limit)

        if new_exposure > limit:
            return (
                False,
                f"pos_limit: {asset} exposure {new_exposure:.2f} > limit {limit:.2f}",
            )

        total_after = self.gross_exposure() + intent.amount_in
        if total_after > self.total_limit:
            return (
                False,
                f"total_limit: gross {total_after:.2f} > limit {self.total_limit:.2f}",
            )

        return True, f"pos_ok: {asset}={new_exposure:.2f}/{limit:.2f} total={total_after:.2f}/{self.total_limit:.2f}"


# ── Margin Monitor ────────────────────────────────────────────────

class MarginMonitor:
    """Tracks maintenance margin utilization and publishes warnings.

    Subscribes to ``market.snapshot`` for mark-to-market updates.
    Publishes ``compliance.margin_warning`` when utilization exceeds
    the configurable threshold.
    """

    def __init__(
        self,
        bus: Bus,
        portfolio: PortfolioTracker,
        total_capital: float = 10_000.0,
        maintenance_pct: float = 0.10,
        warning_threshold: float = 0.80,
        margin_overrides: dict[str, float] | None = None,
    ):
        self.bus = bus
        self.portfolio = portfolio
        self.total_capital = total_capital
        self.maintenance_pct = maintenance_pct
        self.warning_threshold = warning_threshold
        self.margin_overrides: dict[str, float] = margin_overrides or {}
        bus.subscribe("market.snapshot", self._on_snapshot)

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        util = self.margin_utilization()
        if util > self.warning_threshold:
            log.warning(
                "Margin utilization %.1f%% exceeds threshold %.1f%%",
                util * 100,
                self.warning_threshold * 100,
            )
            await self.bus.publish(
                "compliance.margin_warning",
                {
                    "utilization": round(util, 4),
                    "threshold": self.warning_threshold,
                    "total_capital": self.total_capital,
                },
            )

    def margin_required(self) -> float:
        """Total maintenance margin across all positions."""
        total = 0.0
        for asset, pos in self.portfolio.positions.items():
            if pos.quantity < 1e-9:
                continue
            price = self.portfolio.last_prices.get(asset, pos.avg_entry)
            pct = self.margin_overrides.get(asset, self.maintenance_pct)
            total += pos.quantity * price * pct
        return total

    def margin_utilization(self) -> float:
        """Current margin used as a fraction of total capital."""
        if self.total_capital <= 0:
            return 0.0
        return self.margin_required() / self.total_capital

    def margin_call_risk(self) -> bool:
        """True if margin utilization exceeds 80 % of capital."""
        return self.margin_utilization() > 0.80


# ── Reconciler ────────────────────────────────────────────────────

class Reconciler:
    """Compares internal position state against an external source.

    In simulation mode, compares the ``PortfolioTracker`` ledger against
    the Auditor's SQLite ``reports`` table to detect discrepancies.
    """

    def __init__(
        self,
        portfolio: PortfolioTracker,
        db_conn=None,
        interval_s: float = 60.0,
    ):
        self.portfolio = portfolio
        self.db_conn = db_conn
        self.interval_s = interval_s
        self._running = False

    async def reconcile(self) -> dict:
        """Return ``{matches: bool, discrepancies: list[dict]}``."""
        discrepancies: list[dict] = []

        if self.db_conn is None:
            return {"matches": True, "discrepancies": discrepancies}

        # Build positions from SQLite reports
        cursor = self.db_conn.execute(
            "SELECT asset, side, quantity FROM reports WHERE status='filled'"
        )
        db_positions: dict[str, float] = {}
        for asset, side, qty in cursor.fetchall():
            if not asset:
                continue
            db_positions.setdefault(asset, 0.0)
            if side == "buy":
                db_positions[asset] += qty
            elif side == "sell":
                db_positions[asset] -= qty

        # Compare against PortfolioTracker
        all_assets = set(db_positions.keys()) | set(self.portfolio.positions.keys())
        for asset in all_assets:
            internal_qty = self.portfolio.get(asset).quantity
            external_qty = max(0.0, db_positions.get(asset, 0.0))
            diff = abs(internal_qty - external_qty)
            if diff > 1e-6:
                severity = "HIGH" if diff / max(internal_qty, external_qty, 1e-9) > 0.01 else "LOW"
                d = {
                    "asset": asset,
                    "internal_qty": round(internal_qty, 8),
                    "external_qty": round(external_qty, 8),
                    "diff": round(diff, 8),
                    "severity": severity,
                }
                discrepancies.append(d)
                log.warning("Reconciliation discrepancy [%s]: %s", severity, d)

        return {"matches": len(discrepancies) == 0, "discrepancies": discrepancies}

    async def run(self) -> None:
        """Run periodic reconciliation until stopped."""
        self._running = True
        while self._running:
            try:
                result = await self.reconcile()
                if not result["matches"]:
                    log.warning(
                        "Reconciliation found %d discrepancies",
                        len(result["discrepancies"]),
                    )
                else:
                    log.debug("Reconciliation OK")
            except Exception:
                log.exception("Reconciliation error")
            await asyncio.sleep(self.interval_s)

    def stop(self) -> None:
        self._running = False


# ── Data Quality Monitor ──────────────────────────────────────────

class DataQualityMonitor:
    """Monitors market data feed quality per asset.

    Detects stale feeds, bad ticks (> 10 % jump), zero prices,
    and duplicate timestamps.  Subscribes to ``market.snapshot``
    and publishes ``data.quality_alert`` on anomaly detection.
    """

    def __init__(
        self,
        bus: Bus,
        stale_threshold_s: float = 30.0,
        jump_threshold_pct: float = 0.10,
        check_interval_s: float = 15.0,
    ):
        self.bus = bus
        self.stale_threshold_s = stale_threshold_s
        self.jump_threshold_pct = jump_threshold_pct
        self.check_interval_s = check_interval_s

        # Per-asset tracking
        self._last_update: dict[str, float] = {}
        self._last_price: dict[str, float] = {}
        self._tick_count: dict[str, int] = {}
        self._anomalies: dict[str, list[dict]] = {}
        self._last_ts: dict[str, float] = {}
        self._running = False

        bus.subscribe("market.snapshot", self._on_snapshot)

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        now = time.time()
        for asset, price in snap.prices.items():
            self._tick_count[asset] = self._tick_count.get(asset, 0) + 1
            anomalies = self._anomalies.setdefault(asset, [])

            # Zero price
            if price <= 0:
                a = {"type": "zero_price", "ts": now, "asset": asset}
                anomalies.append(a)
                await self._alert(a)
                continue

            # Bad tick — price jump > threshold
            prev = self._last_price.get(asset)
            if prev and prev > 0:
                change = abs(price - prev) / prev
                if change > self.jump_threshold_pct:
                    a = {
                        "type": "bad_tick",
                        "ts": now,
                        "asset": asset,
                        "prev": prev,
                        "current": price,
                        "change_pct": round(change, 4),
                    }
                    anomalies.append(a)
                    await self._alert(a)

            # Duplicate timestamp
            if asset in self._last_ts and snap.ts == self._last_ts[asset]:
                a = {"type": "duplicate_ts", "ts": now, "asset": asset, "snap_ts": snap.ts}
                anomalies.append(a)
                await self._alert(a)

            self._last_price[asset] = price
            self._last_update[asset] = now
            self._last_ts[asset] = snap.ts

    async def _alert(self, anomaly: dict) -> None:
        log.warning("Data quality anomaly: %s", anomaly)
        await self.bus.publish("data.quality_alert", anomaly)

    def health_report(self) -> dict:
        """Per-asset feed health summary."""
        now = time.time()
        report: dict[str, dict] = {}
        for asset in set(self._tick_count.keys()) | set(self._last_update.keys()):
            last_upd = self._last_update.get(asset, 0.0)
            age = now - last_upd if last_upd else float("inf")
            status = "ok"
            if age > self.stale_threshold_s:
                status = "stale"
            if any(
                a["type"] in ("zero_price", "bad_tick")
                for a in self._anomalies.get(asset, [])[-5:]
            ):
                status = "degraded"
            report[asset] = {
                "status": status,
                "last_update": round(last_upd, 3),
                "seconds_since_update": round(age, 1),
                "tick_count": self._tick_count.get(asset, 0),
                "anomalies": len(self._anomalies.get(asset, [])),
            }
        return report

    async def run(self) -> None:
        """Periodic health check — publishes alerts for stale feeds."""
        self._running = True
        while self._running:
            await asyncio.sleep(self.check_interval_s)
            now = time.time()
            for asset, last in list(self._last_update.items()):
                if now - last > self.stale_threshold_s:
                    a = {
                        "type": "stale_feed",
                        "ts": now,
                        "asset": asset,
                        "seconds_since": round(now - last, 1),
                    }
                    await self._alert(a)

    def stop(self) -> None:
        self._running = False


# ── Combined Compliance Check ─────────────────────────────────────

def compliance_check(
    wash_detector: WashTradingDetector,
    margin_monitor: MarginMonitor,
):
    """Returns a risk check function compatible with ``RiskAgent``.

    Combines wash-trade detection and margin monitoring into a single
    ``(intent) -> (ok, reason)`` callable.
    """

    def check(intent: TradeIntent) -> tuple[bool, str]:
        # Wash trade check
        is_w, w_reason = wash_detector.is_wash(intent)
        if is_w:
            return False, w_reason

        # Margin check
        if margin_monitor.margin_call_risk():
            util = margin_monitor.margin_utilization()
            return (
                False,
                f"margin_call_risk: utilization={util:.2%} > 80%",
            )

        return True, "compliance_ok"

    return check
