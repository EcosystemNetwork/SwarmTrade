"""Advanced execution models — Almgren-Chriss optimal scheduling, iceberg orders,
market impact estimation, and transaction cost analysis (TCA).

All models are stdlib-only and integrate via the async Bus.

Execution flow integration:
    exec.go      -> IcebergExecutor intercepts large orders, splits into slices
    exec.report  -> ExecutionQualityTracker records fills for TCA
    market.snapshot -> ExecutionQualityTracker updates arrival prices
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .core import Bus, TradeIntent, ExecutionReport, MarketSnapshot, QUOTE_ASSETS

log = logging.getLogger("swarm.micro")


# ---------------------------------------------------------------------------
# Market Impact Model
# ---------------------------------------------------------------------------
class MarketImpactModel:
    """Estimates temporary and permanent market impact using Kyle's lambda model.

    temporary_impact:  sigma * sqrt(q / V)  (square-root law)
    permanent_impact:  gamma * (q / V)      (linear information leakage)
    """

    DEFAULT_GAMMA = 0.1   # permanent impact coefficient
    DEFAULT_KAPPA = 0.5   # temporary impact coefficient

    def __init__(self, gamma: float = DEFAULT_GAMMA, kappa: float = DEFAULT_KAPPA):
        self.gamma = gamma
        self.kappa = kappa

    def temporary_impact(self, quantity: float, volume: float,
                         volatility: float) -> float:
        """Temporary price impact in fractional terms (e.g. 0.001 = 10 bps).

        Uses square-root model: kappa * sigma * sqrt(q / V).
        """
        if volume <= 0 or quantity <= 0:
            return 0.0
        participation = quantity / volume
        return self.kappa * volatility * math.sqrt(participation)

    def permanent_impact(self, quantity: float, volume: float) -> float:
        """Permanent price impact in fractional terms.

        Linear model: gamma * (q / V).
        """
        if volume <= 0 or quantity <= 0:
            return 0.0
        return self.gamma * (quantity / volume)

    def total_cost(self, quantity: float, volume: float,
                   volatility: float, n_slices: int = 1) -> float:
        """Total expected execution cost in fractional terms when splitting
        into n_slices equal child orders.

        Each slice has quantity/n_slices, so temporary impact is reduced
        by sqrt(n_slices), but permanent impact accumulates linearly.
        """
        if n_slices < 1:
            n_slices = 1
        q_slice = quantity / n_slices
        temp = sum(
            self.temporary_impact(q_slice, volume, volatility)
            for _ in range(n_slices)
        )
        perm = self.permanent_impact(quantity, volume)
        return temp + perm


# ---------------------------------------------------------------------------
# Almgren-Chriss Optimal Execution
# ---------------------------------------------------------------------------
class AlmgrenChrissModel:
    """Optimal execution trajectory minimising execution cost + timing risk.

    The model balances:
    - Market impact (faster -> more impact)
    - Timing risk   (slower -> more variance exposure)

    Parameters
    ----------
    total_quantity : float
        Total quantity to execute.
    urgency : float
        Risk aversion in [0, 1]. 0 = patient (TWAP-like), 1 = aggressive.
    volatility : float
        Annualised volatility of the asset (fractional, e.g. 0.60 for 60%).
    daily_volume : float
        Average daily volume in base units.
    market_impact_coeff : float
        Temporary impact scaling (kappa in the impact model).
    """

    def __init__(self, total_quantity: float, urgency: float,
                 volatility: float, daily_volume: float,
                 market_impact_coeff: float = 0.5):
        self.total_quantity = total_quantity
        self.urgency = max(0.0, min(1.0, urgency))
        self.volatility = volatility
        self.daily_volume = daily_volume
        self.impact_coeff = market_impact_coeff

    def optimal_trajectory(self, n_periods: int = 10) -> list[float]:
        """Compute the optimal execution schedule across n_periods.

        Returns a list of quantities to trade per period, summing to
        total_quantity.

        High urgency -> front-loaded (more in early periods).
        Low urgency  -> uniform (TWAP-like).
        """
        if n_periods <= 0:
            return []
        if n_periods == 1:
            return [self.total_quantity]

        # Decay parameter from urgency: higher urgency = faster decay
        # kappa_tilde controls the curvature of the trajectory
        # At urgency=0 we get uniform, at urgency=1 strongly front-loaded
        kappa_tilde = self.urgency * 3.0  # scale factor

        if kappa_tilde < 1e-6:
            # Uniform / TWAP
            qty = self.total_quantity / n_periods
            return [qty] * n_periods

        # Exponential decay schedule: w_j = exp(-kappa * j / n)
        weights: list[float] = []
        for j in range(n_periods):
            w = math.exp(-kappa_tilde * j / (n_periods - 1))
            weights.append(w)

        total_w = sum(weights)
        trajectory = [self.total_quantity * w / total_w for w in weights]
        return trajectory

    def expected_cost(self) -> float:
        """Expected execution cost in basis points.

        Combines temporary impact (proportional to participation rate)
        with timing risk penalty.
        """
        if self.daily_volume <= 0:
            return 0.0
        participation = self.total_quantity / self.daily_volume

        # Temporary impact cost
        temp_cost = self.impact_coeff * self.volatility * math.sqrt(participation)

        # Timing risk: variance penalty scaled by (1 - urgency)
        # More patient execution -> higher risk exposure
        patience = 1.0 - self.urgency
        risk_cost = 0.5 * patience * self.volatility * math.sqrt(participation)

        total_bps = (temp_cost + risk_cost) * 10_000
        return round(total_bps, 2)


# ---------------------------------------------------------------------------
# Iceberg Order
# ---------------------------------------------------------------------------
@dataclass
class IcebergOrder:
    total_quantity: float
    visible_quantity: float
    filled: float = 0.0
    slices: list[dict[str, Any]] = field(default_factory=list)
    status: str = "pending"  # pending | active | filled | cancelled

    @property
    def remaining(self) -> float:
        return max(0.0, self.total_quantity - self.filled)

    @property
    def progress(self) -> float:
        if self.total_quantity <= 0:
            return 1.0
        return self.filled / self.total_quantity


# ---------------------------------------------------------------------------
# Iceberg Executor
# ---------------------------------------------------------------------------
class IcebergExecutor:
    """Splits large orders into hidden slices to reduce market impact.

    Subscribes to ``exec.go`` and intercepts orders above a USD threshold.
    Each parent order is broken into child slices of ``show_ratio`` visible
    size, emitted with randomised delays to avoid detection.
    """

    def __init__(self, bus: Bus, threshold_usd: float = 1500.0,
                 show_ratio: float = 0.2,
                 impact_model: MarketImpactModel | None = None):
        self.bus = bus
        self.threshold_usd = threshold_usd
        self.show_ratio = max(0.01, min(1.0, show_ratio))
        self.impact = impact_model or MarketImpactModel()
        self._active: dict[str, IcebergOrder] = {}   # parent_id -> IcebergOrder
        self._prices: dict[str, float] = {}

        bus.subscribe("exec.go", self._on_exec_go)
        bus.subscribe("exec.report", self._on_child_report)
        bus.subscribe("market.snapshot", self._on_snapshot)

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        self._prices.update(snap.prices)

    async def _on_exec_go(self, intent: TradeIntent) -> None:
        """Intercept large orders and break them into iceberg slices."""
        if intent.amount_in < self.threshold_usd:
            return  # small order, let it pass through normally

        # Determine asset
        if intent.asset_in.upper() in QUOTE_ASSETS:
            asset = intent.asset_out
        else:
            asset = intent.asset_in

        total_qty = intent.amount_in
        visible_qty = total_qty * self.show_ratio

        iceberg = IcebergOrder(
            total_quantity=total_qty,
            visible_quantity=visible_qty,
        )
        iceberg.status = "active"
        self._active[intent.id] = iceberg

        log.info("ICEBERG %s: splitting %.2f USD into slices of %.2f (show=%.0f%%)",
                 intent.id, total_qty, visible_qty, self.show_ratio * 100)

        # Publish initial progress
        await self.bus.publish("exec.iceberg", {
            "parent_id": intent.id,
            "asset": asset,
            "total_quantity": total_qty,
            "visible_quantity": visible_qty,
            "filled": 0.0,
            "progress": 0.0,
            "status": "active",
            "slices_planned": math.ceil(1.0 / self.show_ratio),
        })

        # Spawn slice emission as background task
        asyncio.create_task(self._emit_slices(intent, iceberg, asset))

    async def _emit_slices(self, parent: TradeIntent, iceberg: IcebergOrder,
                           asset: str) -> None:
        """Emit child intent slices with randomised delays."""
        remaining = iceberg.total_quantity
        slice_num = 0

        while remaining > 1e-6 and iceberg.status == "active":
            slice_qty = min(iceberg.visible_quantity, remaining)
            slice_num += 1

            child = TradeIntent.new(
                asset_in=parent.asset_in,
                asset_out=parent.asset_out,
                amount_in=slice_qty,
                min_out=parent.min_out * (slice_qty / parent.amount_in) if parent.min_out else 0,
                ttl=parent.ttl,
                supporting=parent.supporting,
            )

            slice_record = {
                "child_id": child.id,
                "parent_id": parent.id,
                "slice_num": slice_num,
                "quantity": slice_qty,
                "ts": time.time(),
            }
            iceberg.slices.append(slice_record)

            log.info("ICEBERG %s slice #%d: %.2f USD (child %s)",
                     parent.id, slice_num, slice_qty, child.id)

            # Publish child intent through normal flow
            await self.bus.publish("intent.new", child)
            await self.bus.publish("risk.verdict", child)

            remaining -= slice_qty

            # Randomised delay between slices (2-8 seconds)
            if remaining > 1e-6:
                delay = random.uniform(2.0, 8.0)
                await asyncio.sleep(delay)

        if iceberg.status == "active":
            iceberg.status = "filled"

    async def _on_child_report(self, rep: ExecutionReport) -> None:
        """Track fill progress of iceberg child orders."""
        for parent_id, iceberg in self._active.items():
            for s in iceberg.slices:
                if s.get("child_id") == rep.intent_id and rep.status == "filled":
                    fill_amt = s["quantity"]
                    iceberg.filled += fill_amt
                    s["filled"] = True
                    s["fill_price"] = rep.fill_price

                    # Determine asset
                    asset = rep.asset or "unknown"

                    await self.bus.publish("exec.iceberg", {
                        "parent_id": parent_id,
                        "asset": asset,
                        "total_quantity": iceberg.total_quantity,
                        "visible_quantity": iceberg.visible_quantity,
                        "filled": iceberg.filled,
                        "progress": round(iceberg.progress, 4),
                        "status": iceberg.status,
                        "slices_completed": sum(1 for sl in iceberg.slices if sl.get("filled")),
                    })
                    return


# ---------------------------------------------------------------------------
# Execution Quality Tracker (TCA)
# ---------------------------------------------------------------------------
class ExecutionQualityTracker:
    """Tracks execution quality metrics and produces Transaction Cost Analysis.

    Subscribes to ``exec.report`` for fill data and ``market.snapshot`` for
    arrival price benchmarks.
    """

    def __init__(self, bus: Bus):
        self.bus = bus
        self._arrival_prices: dict[str, float] = {}   # asset -> price at intent time
        self._fills: list[dict[str, Any]] = []
        self._venue_stats: dict[str, dict[str, Any]] = {}
        self._last_prices: dict[str, float] = {}
        self._tca_interval: float = 30.0  # publish TCA every N seconds

        bus.subscribe("exec.report", self._on_report)
        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("intent.new", self._on_intent)
        bus.subscribe("sor.routed", self._on_routed)

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        self._last_prices.update(snap.prices)

    async def _on_intent(self, intent: TradeIntent) -> None:
        """Capture arrival price at intent creation time."""
        if intent.asset_in.upper() in QUOTE_ASSETS:
            asset = intent.asset_out
        else:
            asset = intent.asset_in

        price = self._last_prices.get(asset.upper())
        if price:
            self._arrival_prices[intent.id] = price

    async def _on_routed(self, msg: dict) -> None:
        """Record which venue was selected for later per-venue stats."""
        intent_id = msg.get("intent_id", "")
        venue = msg.get("venue", "unknown")
        # Pre-populate venue entry
        if venue not in self._venue_stats:
            self._venue_stats[venue] = {
                "fills": 0, "total_slippage_bps": 0.0,
                "total_impact_bps": 0.0, "total_fees_bps": 0.0,
            }

    async def _on_report(self, rep: ExecutionReport) -> None:
        """Record fill and compute execution quality metrics."""
        if rep.status != "filled" or rep.fill_price is None:
            return

        asset = rep.asset or "unknown"
        arrival = self._arrival_prices.get(rep.intent_id)
        mid = self._last_prices.get(asset.upper(), rep.fill_price)

        if arrival is None:
            arrival = mid  # fallback

        # Implementation shortfall: arrival vs fill
        if arrival > 0:
            if rep.side == "buy":
                shortfall_bps = (rep.fill_price - arrival) / arrival * 10_000
            else:
                shortfall_bps = (arrival - rep.fill_price) / arrival * 10_000
        else:
            shortfall_bps = 0.0

        # Slippage from mid
        slippage_bps = (rep.realized_slippage or 0.0) * 10_000

        # Estimate market impact (fill vs mid at report time)
        if mid > 0:
            impact_bps = abs(rep.fill_price - mid) / mid * 10_000
        else:
            impact_bps = 0.0

        # Fee cost in bps of notional
        notional = rep.quantity * rep.fill_price if rep.quantity > 0 else 1.0
        fee_bps = (rep.fee_usd / max(notional, 1e-9)) * 10_000

        fill_record = {
            "ts": time.time(),
            "intent_id": rep.intent_id,
            "asset": asset,
            "side": rep.side,
            "fill_price": rep.fill_price,
            "arrival_price": arrival,
            "mid_price": mid,
            "quantity": rep.quantity,
            "slippage_bps": round(slippage_bps, 2),
            "impact_bps": round(impact_bps, 2),
            "shortfall_bps": round(shortfall_bps, 2),
            "fee_bps": round(fee_bps, 2),
            "fee_usd": rep.fee_usd,
        }
        self._fills.append(fill_record)
        if len(self._fills) > 10000:
            self._fills = self._fills[-5000:]

        log.info("TCA %s %s: slip=%.1fbps impact=%.1fbps shortfall=%.1fbps fee=%.1fbps",
                 rep.intent_id, rep.side, slippage_bps, impact_bps,
                 shortfall_bps, fee_bps)

    def tca_report(self) -> dict:
        """Generate Transaction Cost Analysis report.

        Returns summary statistics across all tracked fills.
        """
        if not self._fills:
            return {
                "total_fills": 0,
                "avg_slippage_bps": 0.0,
                "avg_market_impact_bps": 0.0,
                "avg_timing_cost_bps": 0.0,
                "implementation_shortfall": 0.0,
                "venue_performance": {},
            }

        n = len(self._fills)
        total_slip = sum(f["slippage_bps"] for f in self._fills)
        total_impact = sum(f["impact_bps"] for f in self._fills)
        total_shortfall = sum(f["shortfall_bps"] for f in self._fills)
        total_fee = sum(f["fee_bps"] for f in self._fills)

        # Timing cost: shortfall minus impact (the cost of waiting)
        avg_timing = (total_shortfall - total_impact) / n

        # Per-venue aggregation from sor.routed data
        venue_perf: dict[str, dict[str, Any]] = {}
        for vs_name, vs_data in self._venue_stats.items():
            venue_perf[vs_name] = {
                "fills": vs_data["fills"],
                "avg_slippage_bps": round(
                    vs_data["total_slippage_bps"] / max(1, vs_data["fills"]), 2
                ),
            }

        return {
            "total_fills": n,
            "avg_slippage_bps": round(total_slip / n, 2),
            "avg_market_impact_bps": round(total_impact / n, 2),
            "avg_timing_cost_bps": round(avg_timing, 2),
            "avg_fee_bps": round(total_fee / n, 2),
            "implementation_shortfall": round(total_shortfall / n, 2),
            "venue_performance": venue_perf,
            "recent_fills": self._fills[-10:],
        }

    async def run(self) -> None:
        """Periodically publish TCA updates."""
        while True:
            await asyncio.sleep(self._tca_interval)
            if self._fills:
                report = self.tca_report()
                await self.bus.publish("tca.update", report)
                log.info("TCA update: %d fills, avg_shortfall=%.1fbps",
                         report["total_fills"],
                         report["implementation_shortfall"])
