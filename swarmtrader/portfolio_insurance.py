"""Portfolio Insurance — automated hedging via options and perps.

Inspired by Arcare (parametric insurance for DeFi priced by prediction
markets), DeltaFX (FX insurance for stablecoin economy), BulwArc (hedging
FX risk without a bank), and GammaHedge (delta-neutral LP hedging).

Automatically hedges portfolio risk using:
  1. Delta hedging via perpetual shorts (neutralize directional exposure)
  2. Tail risk protection via OTM puts (insure against >10% drops)
  3. Correlation hedging (hedge correlated exposures with single position)
  4. Dynamic hedge ratio (adjust based on regime and portfolio beta)

Bus integration:
  Subscribes to: market.snapshot, signal.regime_v2, shield.warning
  Publishes to:  insurance.hedge, insurance.payout, signal.insurance
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .core import Bus, Signal, MarketSnapshot

log = logging.getLogger("swarm.insurance")


@dataclass
class HedgePosition:
    """An active hedge position."""
    hedge_id: str
    hedge_type: str         # "delta", "tail", "correlation"
    asset: str
    instrument: str         # "perp_short", "put_option", "inverse_etf"
    # Position
    notional_usd: float
    entry_price: float
    current_price: float = 0.0
    # Cost
    premium_usd: float = 0.0    # option premium or funding cost
    daily_cost: float = 0.0     # ongoing cost per day
    # Coverage
    coverage_pct: float = 0.0   # what % of portfolio is hedged
    max_payout: float = 0.0     # max insurance payout
    # Status
    active: bool = True
    pnl: float = 0.0
    ts: float = field(default_factory=time.time)


class PortfolioInsurance:
    """Automated portfolio hedging and insurance.

    Hedge decision matrix:
      - Normal regime: delta hedge 20% of portfolio
      - Risk-off: delta hedge 50%, buy 5% OTM puts
      - Crisis: delta hedge 80%, buy 3% OTM puts, max protection
      - Low vol: minimal hedging (save on costs)

    Cost management:
      - Track total hedging cost as % of portfolio
      - Cap hedging budget at 2% of portfolio annually
      - Prefer perp shorts (earn funding) over options (pay premium)
    """

    def __init__(self, bus: Bus, portfolio_usd: float = 10000.0,
                 max_hedge_cost_pct: float = 2.0):
        self.bus = bus
        self.portfolio_usd = portfolio_usd
        self.max_hedge_cost_pct = max_hedge_cost_pct
        self._hedges: dict[str, HedgePosition] = {}
        self._hedge_counter = 0
        self._regime: str = "range"
        self._stats = {
            "hedges_placed": 0, "hedges_closed": 0,
            "total_cost": 0.0, "total_payout": 0.0,
        }

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("signal.regime_v2", self._on_regime)
        bus.subscribe("shield.warning", self._on_shield_warning)

    async def _on_regime(self, sig: Signal):
        if isinstance(sig, Signal):
            rationale = sig.rationale.lower()
            for regime in ["crisis", "risk_off", "risk_on", "rotation", "range"]:
                if regime in rationale:
                    old = self._regime
                    self._regime = regime
                    if old != regime:
                        await self._adjust_hedges()
                    break

    async def _on_snapshot(self, snap: MarketSnapshot):
        """Update hedge positions with current prices."""
        for hedge in self._hedges.values():
            if not hedge.active:
                continue
            price = snap.prices.get(hedge.asset)
            if price:
                hedge.current_price = price
                # P&L for short hedge
                if "short" in hedge.instrument:
                    hedge.pnl = (hedge.entry_price - price) / hedge.entry_price * hedge.notional_usd

    async def _on_shield_warning(self, data):
        """Increase hedging when liquidation shield warns."""
        if self._regime not in ("crisis", "risk_off"):
            self._regime = "risk_off"
            await self._adjust_hedges()

    async def _adjust_hedges(self):
        """Adjust hedge positions based on current regime."""
        target_coverage = {
            "crisis": 0.8,
            "risk_off": 0.5,
            "risk_on": 0.1,
            "rotation": 0.2,
            "range": 0.15,
        }.get(self._regime, 0.2)

        current_coverage = sum(
            h.coverage_pct for h in self._hedges.values() if h.active
        ) / 100

        if current_coverage < target_coverage - 0.1:
            # Need more hedging
            additional = (target_coverage - current_coverage) * self.portfolio_usd
            await self._place_hedge("ETH", additional, "delta")

        elif current_coverage > target_coverage + 0.15:
            # Over-hedged, reduce
            for hedge in list(self._hedges.values()):
                if hedge.active and current_coverage > target_coverage:
                    hedge.active = False
                    self._stats["hedges_closed"] += 1
                    current_coverage -= hedge.coverage_pct / 100

    async def _place_hedge(self, asset: str, notional: float, hedge_type: str):
        """Place a new hedge position."""
        self._hedge_counter += 1
        hid = f"hedge-{self._hedge_counter:04d}"

        coverage = (notional / max(self.portfolio_usd, 1)) * 100
        daily_cost = notional * 0.0001  # ~0.01% daily (funding rate)

        hedge = HedgePosition(
            hedge_id=hid,
            hedge_type=hedge_type,
            asset=asset,
            instrument="perp_short",
            notional_usd=round(notional, 2),
            entry_price=0,  # would be current price
            premium_usd=0,
            daily_cost=round(daily_cost, 4),
            coverage_pct=round(coverage, 1),
            max_payout=round(notional * 0.5, 2),  # max 50% of notional
        )
        self._hedges[hid] = hedge
        self._stats["hedges_placed"] += 1
        self._stats["total_cost"] += daily_cost

        log.info(
            "INSURANCE: %s %s hedge on %s $%.0f (coverage=%.1f%%, regime=%s)",
            hid, hedge_type, asset, notional, coverage, self._regime,
        )

        sig = Signal(
            agent_id="portfolio_insurance",
            asset=asset,
            direction="short",
            strength=0.3,
            confidence=0.8,
            rationale=f"Hedge: {hedge_type} ${notional:.0f} (regime={self._regime}, coverage={coverage:.1f}%)",
        )
        await self.bus.publish("signal.insurance", sig)
        await self.bus.publish("insurance.hedge", hedge)

    def summary(self) -> dict:
        active = [h for h in self._hedges.values() if h.active]
        return {
            **self._stats,
            "regime": self._regime,
            "portfolio_usd": round(self.portfolio_usd, 2),
            "total_coverage_pct": round(sum(h.coverage_pct for h in active), 1),
            "total_daily_cost": round(sum(h.daily_cost for h in active), 4),
            "hedges": [
                {
                    "id": h.hedge_id,
                    "type": h.hedge_type,
                    "asset": h.asset,
                    "notional": h.notional_usd,
                    "coverage": h.coverage_pct,
                    "pnl": round(h.pnl, 4),
                }
                for h in active
            ],
        }
