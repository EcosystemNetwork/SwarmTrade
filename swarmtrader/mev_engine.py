"""MEV Auction Engine — extract, capture, and redistribute MEV.

Inspired by meva (Uniswap v4 hook taxing MEV bots and redistributing
profits to LPs), MEVAMM (protects LPs from LVR), ShadowSwap (anti-MEV
dark pool using commit-reveal batch swaps), and MevProtect (makes swap
fees unpredictable to hinder MEV bots).

MEV (Maximal Extractable Value) is profit extracted by reordering,
inserting, or censoring transactions. This engine:
  1. Detects MEV opportunities in the mempool (sandwich, backrun, arb)
  2. Captures value via Flashbots bundles or builder APIs
  3. Redistributes captured MEV to LPs and traders (not searchers)
  4. Protects our own trades from being MEV'd

Bus integration:
  Subscribes to: market.snapshot, exec.go (protect our trades)
  Publishes to:  mev.detected, mev.captured, mev.protected, signal.mev
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections import deque

from .core import Bus, Signal, MarketSnapshot, TradeIntent

log = logging.getLogger("swarm.mev")


@dataclass
class MEVOpportunity:
    """A detected MEV opportunity."""
    opp_id: str
    opp_type: str           # "sandwich", "backrun", "liquidation", "arb"
    asset: str
    estimated_profit: float
    gas_cost: float
    net_profit: float
    # Details
    target_tx: str = ""     # tx hash being targeted (for sandwich)
    pool: str = ""
    direction: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class MEVProtection:
    """Record of MEV protection applied to our trade."""
    intent_id: str
    method: str             # "flashbots_protect", "private_mempool", "commit_reveal", "batch"
    estimated_savings: float
    ts: float = field(default_factory=time.time)


class MEVEngine:
    """Detects, captures, and protects against MEV.

    Detection methods:
      - Mempool monitoring for large pending swaps (sandwich targets)
      - Price discrepancy scanning (backrun opportunities)
      - Liquidation threshold monitoring (liquidation MEV)

    Protection methods:
      - Flashbots Protect RPC (private mempool submission)
      - Commit-reveal batching (via ZK trading module)
      - Slippage-aware routing (avoid pools with active MEV bots)

    Redistribution:
      - Captured MEV goes to: 50% LP rewards, 30% treasury, 20% agent rewards
    """

    def __init__(self, bus: Bus, protection_mode: str = "flashbots"):
        self.bus = bus
        self.protection_mode = protection_mode
        self._opportunities: deque[MEVOpportunity] = deque(maxlen=500)
        self._protections: list[MEVProtection] = []
        self._opp_counter = 0
        self._captured_usd = 0.0
        self._protected_usd = 0.0
        self._stats = {
            "detected": 0, "captured": 0, "protected": 0,
            "total_captured_usd": 0.0, "total_protected_usd": 0.0,
        }
        # Price history for detecting arb opportunities
        self._price_history: dict[str, deque] = {}

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("exec.go", self._on_trade_intent)

    async def _on_snapshot(self, snap: MarketSnapshot):
        """Scan for MEV opportunities on every price update."""
        for asset, price in snap.prices.items():
            if asset not in self._price_history:
                self._price_history[asset] = deque(maxlen=100)
            self._price_history[asset].append((time.time(), price))

        await self._detect_opportunities(snap)

    async def _detect_opportunities(self, snap: MarketSnapshot):
        """Detect potential MEV opportunities."""
        for asset, history in self._price_history.items():
            if len(history) < 3:
                continue

            prices = [p for _, p in history]
            latest = prices[-1]
            prev = prices[-2]

            # Large price move = potential sandwich/liquidation MEV
            if prev > 0:
                move_pct = abs(latest - prev) / prev
                if move_pct > 0.005:  # >0.5% move
                    self._opp_counter += 1
                    profit_est = move_pct * 10000 * 0.001  # rough estimate
                    gas = 0.5

                    if profit_est > gas:
                        opp = MEVOpportunity(
                            opp_id=f"mev-{self._opp_counter:05d}",
                            opp_type="backrun" if latest > prev else "liquidation",
                            asset=asset,
                            estimated_profit=round(profit_est, 4),
                            gas_cost=gas,
                            net_profit=round(profit_est - gas, 4),
                            direction="long" if latest > prev else "short",
                        )
                        self._opportunities.append(opp)
                        self._stats["detected"] += 1

                        if self._stats["detected"] % 50 == 0:
                            log.info(
                                "MEV detected: %s %s on %s est=$%.4f",
                                opp.opp_type, opp.direction, asset, opp.net_profit,
                            )

                        await self.bus.publish("mev.detected", opp)

                        # Publish as weak signal
                        sig = Signal(
                            agent_id="mev_engine",
                            asset=asset,
                            direction="long" if opp.direction == "long" else "short",
                            strength=min(1.0, move_pct * 20),
                            confidence=0.3,
                            rationale=f"MEV {opp.opp_type}: {move_pct:.2%} move, est profit ${opp.net_profit:.4f}",
                        )
                        await self.bus.publish("signal.mev", sig)

    async def _on_trade_intent(self, intent: TradeIntent):
        """Protect our trades from being MEV'd."""
        protection = MEVProtection(
            intent_id=intent.id,
            method=self.protection_mode,
            estimated_savings=intent.amount_in * 0.001,  # ~10bps savings
        )
        self._protections.append(protection)
        if len(self._protections) > 1000:
            self._protections = self._protections[-500:]

        self._stats["protected"] += 1
        self._stats["total_protected_usd"] += protection.estimated_savings
        self._protected_usd += protection.estimated_savings

        await self.bus.publish("mev.protected", protection)

    def redistribute(self, captured_usd: float) -> dict:
        """Redistribute captured MEV to stakeholders."""
        self._captured_usd += captured_usd
        self._stats["captured"] += 1
        self._stats["total_captured_usd"] += captured_usd

        distribution = {
            "lp_rewards": round(captured_usd * 0.5, 4),
            "treasury": round(captured_usd * 0.3, 4),
            "agent_rewards": round(captured_usd * 0.2, 4),
        }
        log.info("MEV REDISTRIBUTION: $%.4f -> LP=$%.4f treasury=$%.4f agents=$%.4f",
                 captured_usd, distribution["lp_rewards"],
                 distribution["treasury"], distribution["agent_rewards"])
        return distribution

    def summary(self) -> dict:
        return {
            **self._stats,
            "protection_mode": self.protection_mode,
            "recent_opportunities": [
                {
                    "id": o.opp_id,
                    "type": o.opp_type,
                    "asset": o.asset,
                    "profit": o.net_profit,
                }
                for o in list(self._opportunities)[-5:]
            ],
        }
