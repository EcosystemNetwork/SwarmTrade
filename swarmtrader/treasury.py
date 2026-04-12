"""Autonomous Treasury Management — AI-managed multi-asset treasury.

Inspired by ZeroKey Treasury (agents monitoring exchange rates, rebalancing
holdings, executing payouts), AutoCFO (AI CFO for DAOs automating treasury
with RWA yields), Meridian (autonomous treasury agent for stablecoin conversion),
and Tresora (AI agents that manage treasuries and move money across chains).

Manages the platform's treasury:
  1. Multi-asset allocation (crypto, stablecoins, T-bills, LP positions)
  2. Yield optimization (auto-rotate to highest risk-adjusted yield)
  3. Expense management (agent payments, gas reserves, infrastructure costs)
  4. Runway tracking (months of operating capital remaining)
  5. Auto-rebalance (maintain target allocation weights)
  6. Revenue accounting (track income from fees, arb, strategies)

Bus integration:
  Subscribes to: exec.report, marketplace.fee, vault.deposit
  Publishes to:  treasury.rebalance, treasury.report, treasury.alert
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .core import Bus, ExecutionReport

log = logging.getLogger("swarm.treasury")


@dataclass
class AssetAllocation:
    """Target allocation for a treasury asset."""
    asset: str
    target_pct: float       # target % of treasury
    current_pct: float = 0.0
    current_usd: float = 0.0
    yield_apy: float = 0.0
    drift_pct: float = 0.0  # how far from target


@dataclass
class TreasuryState:
    """Current state of the treasury."""
    total_usd: float = 0.0
    allocations: dict[str, AssetAllocation] = field(default_factory=dict)
    # Revenue tracking
    revenue_today: float = 0.0
    revenue_mtd: float = 0.0
    revenue_total: float = 0.0
    # Expense tracking
    expenses_today: float = 0.0
    expenses_mtd: float = 0.0
    # Runway
    monthly_burn: float = 0.0
    runway_months: float = 0.0
    # Health
    diversification_score: float = 0.0  # 0=concentrated, 1=well-diversified


@dataclass
class RebalanceOrder:
    """A treasury rebalance instruction."""
    order_id: str
    sell_asset: str
    buy_asset: str
    amount_usd: float
    reason: str
    ts: float = field(default_factory=time.time)


class AutonomousTreasury:
    """AI-managed treasury with multi-asset allocation and yield optimization.

    Default allocation targets (configurable):
      - 40% Stablecoins (USDC) — operating capital
      - 25% T-bills (USDY/OUSG) — yield on reserves
      - 20% ETH — core crypto exposure
      - 10% BTC — diversification
      - 5% LP positions — yield farming
    """

    DEFAULT_TARGETS = {
        "USDC": 40.0,
        "USDY": 25.0,
        "ETH": 20.0,
        "BTC": 10.0,
        "LP": 5.0,
    }

    def __init__(self, bus: Bus, initial_capital: float = 0.0,
                 rebalance_threshold_pct: float = 5.0,
                 targets: dict[str, float] | None = None):
        self.bus = bus
        self.state = TreasuryState(total_usd=initial_capital)
        self.rebalance_threshold = rebalance_threshold_pct
        self._targets = targets or dict(self.DEFAULT_TARGETS)
        self._orders: list[RebalanceOrder] = []
        self._order_counter = 0
        self._stats = {
            "rebalances": 0, "revenue_events": 0, "expense_events": 0,
        }

        # Initialize allocations
        for asset, target in self._targets.items():
            self.state.allocations[asset] = AssetAllocation(
                asset=asset, target_pct=target,
                current_usd=initial_capital * (target / 100),
                current_pct=target,
            )

        bus.subscribe("exec.report", self._on_execution)
        bus.subscribe("marketplace.fee", self._on_fee)

    async def _on_execution(self, report: ExecutionReport):
        """Track trading revenue."""
        if report.status == "filled" and report.pnl_estimate:
            pnl = report.pnl_estimate
            if pnl > 0:
                self.state.revenue_today += pnl
                self.state.revenue_mtd += pnl
                self.state.revenue_total += pnl
                self.state.total_usd += pnl
                self._stats["revenue_events"] += 1

    async def _on_fee(self, fee_data):
        """Track marketplace fee revenue."""
        amount = 0
        if isinstance(fee_data, dict):
            amount = fee_data.get("fee_earned", 0)
        elif hasattr(fee_data, "fee_earned"):
            amount = fee_data.fee_earned
        if amount > 0:
            self.state.revenue_today += amount
            self.state.revenue_total += amount

    def record_expense(self, amount: float, category: str = "operations"):
        """Record a treasury expense."""
        self.state.expenses_today += amount
        self.state.expenses_mtd += amount
        self.state.total_usd -= amount
        self._stats["expense_events"] += 1

    def check_rebalance(self) -> list[RebalanceOrder]:
        """Check if any allocations have drifted beyond threshold."""
        if self.state.total_usd <= 0:
            return []

        orders = []
        for asset, alloc in self.state.allocations.items():
            alloc.current_pct = (alloc.current_usd / self.state.total_usd) * 100
            alloc.drift_pct = alloc.current_pct - alloc.target_pct

            if abs(alloc.drift_pct) > self.rebalance_threshold:
                # Need to rebalance
                target_usd = self.state.total_usd * (alloc.target_pct / 100)
                diff = alloc.current_usd - target_usd

                if diff > 0:
                    # Over-allocated: sell this, buy under-allocated
                    under = self._most_underweight()
                    if under and under != asset:
                        self._order_counter += 1
                        order = RebalanceOrder(
                            order_id=f"treas-{self._order_counter:04d}",
                            sell_asset=asset,
                            buy_asset=under,
                            amount_usd=round(abs(diff) * 0.5, 2),  # 50% correction
                            reason=f"{asset} over by {alloc.drift_pct:+.1f}%, {under} under",
                        )
                        orders.append(order)

        if orders:
            self._stats["rebalances"] += len(orders)
            self._orders.extend(orders)
            if len(self._orders) > 200:
                self._orders = self._orders[-100:]

        return orders

    def _most_underweight(self) -> str | None:
        """Find the most underweight asset."""
        most_under = None
        max_drift = 0
        for asset, alloc in self.state.allocations.items():
            if alloc.drift_pct < -max_drift:
                max_drift = abs(alloc.drift_pct)
                most_under = asset
        return most_under

    def update_runway(self, monthly_burn: float):
        """Update runway calculation."""
        self.state.monthly_burn = monthly_burn
        if monthly_burn > 0:
            self.state.runway_months = self.state.total_usd / monthly_burn
        else:
            self.state.runway_months = float("inf")

    def diversification_score(self) -> float:
        """Calculate diversification using Herfindahl index."""
        if not self.state.allocations or self.state.total_usd <= 0:
            return 0.0
        weights = [a.current_usd / self.state.total_usd for a in self.state.allocations.values()]
        hhi = sum(w ** 2 for w in weights)
        n = len(weights)
        # Normalize: 1/n = perfectly diversified, 1 = all in one asset
        score = (1 - hhi) / (1 - 1 / max(n, 1)) if n > 1 else 0
        self.state.diversification_score = round(score, 3)
        return score

    def summary(self) -> dict:
        self.diversification_score()
        return {
            **self._stats,
            "total_usd": round(self.state.total_usd, 2),
            "revenue_total": round(self.state.revenue_total, 2),
            "expenses_mtd": round(self.state.expenses_mtd, 2),
            "runway_months": round(self.state.runway_months, 1) if self.state.runway_months != float("inf") else "infinite",
            "diversification": self.state.diversification_score,
            "allocations": {
                asset: {
                    "target": alloc.target_pct,
                    "current": round(alloc.current_pct, 1),
                    "usd": round(alloc.current_usd, 2),
                    "drift": round(alloc.drift_pct, 1),
                }
                for asset, alloc in self.state.allocations.items()
            },
        }
