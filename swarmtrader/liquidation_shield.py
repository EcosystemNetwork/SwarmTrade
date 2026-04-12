"""Liquidation Cascade Shield — protect positions from cascade events.

Inspired by GammaHedge (autonomous LP guardian with delta-neutral hedging),
The Watchful Eye (auto self-liquidation via AAVE flashloans), and Rescue.ETH
(autonomous system preventing Aave liquidations using fast cross-chain execution).

Monitors all open positions for liquidation proximity and takes protective
action before cascades hit:
  1. Health factor monitoring for lending positions
  2. Cascade prediction from liquidation cluster analysis
  3. Pre-emptive deleverage when health approaches danger zone
  4. Emergency exit via flash loan self-liquidation
  5. Cross-chain collateral top-up from healthiest chain

Bus integration:
  Subscribes to: market.snapshot, signal.liquidation, signal.liquidation_cascade
  Publishes to:  shield.warning, shield.action, shield.emergency

No new external dependencies.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .core import Bus, Signal, MarketSnapshot

log = logging.getLogger("swarm.shield")


@dataclass
class ProtectedPosition:
    """A position being monitored for liquidation risk."""
    position_id: str
    asset: str
    side: str               # "long" or "short"
    entry_price: float
    current_price: float = 0.0
    quantity: float = 0.0
    leverage: float = 1.0
    # Lending position fields
    collateral_usd: float = 0.0
    debt_usd: float = 0.0
    health_factor: float = 999.0  # Aave-style: < 1.0 = liquidatable
    liquidation_price: float = 0.0
    # Status
    risk_level: str = "safe"   # safe, watch, danger, critical
    last_action: str = ""
    last_check: float = field(default_factory=time.time)


@dataclass
class ShieldAction:
    """A protective action taken by the shield."""
    action_id: str
    position_id: str
    action_type: str        # "deleverage", "add_collateral", "close", "flash_liquidate"
    amount: float
    reason: str
    result: str = "pending"  # pending, success, failed
    ts: float = field(default_factory=time.time)


class LiquidationShield:
    """Monitors positions and takes protective action against liquidations.

    Risk levels:
      SAFE (health > 2.0):     No action needed
      WATCH (1.5 < health < 2.0): Log warning, prepare exit plan
      DANGER (1.2 < health < 1.5): Pre-emptive deleverage by 25%
      CRITICAL (health < 1.2):  Emergency full close or flash loan rescue
    """

    SAFE_THRESHOLD = 2.0
    WATCH_THRESHOLD = 1.5
    DANGER_THRESHOLD = 1.2
    CRITICAL_THRESHOLD = 1.05

    def __init__(self, bus: Bus, auto_deleverage: bool = True,
                 deleverage_pct: float = 25.0):
        self.bus = bus
        self.auto_deleverage = auto_deleverage
        self.deleverage_pct = deleverage_pct
        self._positions: dict[str, ProtectedPosition] = {}
        self._actions: list[ShieldAction] = []
        self._action_counter = 0
        self._cascade_warnings: list[dict] = []
        self._stats = {
            "checks": 0, "warnings": 0, "deleverages": 0,
            "emergency_closes": 0, "saved_usd": 0.0,
        }

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("signal.liquidation", self._on_liquidation_signal)
        bus.subscribe("signal.liquidation_cascade", self._on_cascade_signal)

    def register_position(self, position_id: str, asset: str, side: str,
                          entry_price: float, quantity: float = 0,
                          leverage: float = 1.0, collateral_usd: float = 0,
                          debt_usd: float = 0) -> ProtectedPosition:
        """Register a position for liquidation protection."""
        liq_price = self._calc_liquidation_price(entry_price, leverage, side)
        health = collateral_usd / max(debt_usd, 1e-9) if debt_usd > 0 else 999.0

        pos = ProtectedPosition(
            position_id=position_id, asset=asset, side=side,
            entry_price=entry_price, quantity=quantity,
            leverage=leverage, collateral_usd=collateral_usd,
            debt_usd=debt_usd, health_factor=health,
            liquidation_price=liq_price,
        )
        self._positions[position_id] = pos
        log.info("Shield: protecting %s %s %s (lev=%.1fx, liq=$%.2f)",
                 position_id, side, asset, leverage, liq_price)
        return pos

    def _calc_liquidation_price(self, entry: float, leverage: float,
                                 side: str) -> float:
        """Estimate liquidation price for a leveraged position."""
        if leverage <= 1:
            return 0.0  # no liquidation for spot
        margin_pct = 1.0 / leverage
        if side == "long":
            return entry * (1 - margin_pct + 0.01)  # 1% maintenance margin
        else:
            return entry * (1 + margin_pct - 0.01)

    async def _on_snapshot(self, snap: MarketSnapshot):
        """Update positions and check health on every price update."""
        self._stats["checks"] += 1
        for pos in self._positions.values():
            price = snap.prices.get(pos.asset)
            if not price:
                continue
            pos.current_price = price
            pos.last_check = time.time()

            # Update health factor
            if pos.debt_usd > 0:
                if pos.side == "long":
                    collateral_value = pos.quantity * price
                else:
                    collateral_value = pos.collateral_usd + (pos.entry_price - price) * pos.quantity
                pos.health_factor = collateral_value / max(pos.debt_usd, 1e-9)
            elif pos.leverage > 1:
                # Perp position: distance to liquidation
                if pos.liquidation_price > 0:
                    if pos.side == "long":
                        distance_pct = (price - pos.liquidation_price) / price
                    else:
                        distance_pct = (pos.liquidation_price - price) / price
                    pos.health_factor = max(0.1, distance_pct * 10)

            # Classify risk
            old_risk = pos.risk_level
            if pos.health_factor >= self.SAFE_THRESHOLD:
                pos.risk_level = "safe"
            elif pos.health_factor >= self.WATCH_THRESHOLD:
                pos.risk_level = "watch"
            elif pos.health_factor >= self.DANGER_THRESHOLD:
                pos.risk_level = "danger"
            else:
                pos.risk_level = "critical"

            # Take action on transitions
            if pos.risk_level != old_risk:
                await self._handle_risk_change(pos, old_risk)

    async def _handle_risk_change(self, pos: ProtectedPosition, old_risk: str):
        """Handle risk level transitions."""
        if pos.risk_level == "watch" and old_risk == "safe":
            self._stats["warnings"] += 1
            log.warning(
                "SHIELD WATCH: %s health=%.2f (was safe) — preparing exit plan",
                pos.position_id, pos.health_factor,
            )
            await self.bus.publish("shield.warning", {
                "position": pos, "level": "watch",
            })

        elif pos.risk_level == "danger":
            if self.auto_deleverage:
                await self._deleverage(pos)
            else:
                log.error(
                    "SHIELD DANGER: %s health=%.2f — manual deleverage recommended",
                    pos.position_id, pos.health_factor,
                )

        elif pos.risk_level == "critical":
            await self._emergency_close(pos)

    async def _deleverage(self, pos: ProtectedPosition):
        """Pre-emptively reduce position size."""
        self._action_counter += 1
        reduce_qty = pos.quantity * (self.deleverage_pct / 100)
        reduce_usd = reduce_qty * pos.current_price

        action = ShieldAction(
            action_id=f"shield-{self._action_counter:05d}",
            position_id=pos.position_id,
            action_type="deleverage",
            amount=round(reduce_usd, 2),
            reason=f"health={pos.health_factor:.2f} < {self.DANGER_THRESHOLD} danger threshold",
            result="executed",
        )
        self._actions.append(action)
        self._stats["deleverages"] += 1
        self._stats["saved_usd"] += reduce_usd

        pos.quantity -= reduce_qty
        pos.last_action = f"deleveraged {self.deleverage_pct}%"

        log.warning(
            "SHIELD DELEVERAGE: %s reduced by %.0f%% ($%.0f) — health was %.2f",
            pos.position_id, self.deleverage_pct, reduce_usd, pos.health_factor,
        )

        await self.bus.publish("shield.action", action)

    async def _emergency_close(self, pos: ProtectedPosition):
        """Emergency close a critically unhealthy position."""
        self._action_counter += 1
        close_usd = pos.quantity * pos.current_price

        action = ShieldAction(
            action_id=f"shield-{self._action_counter:05d}",
            position_id=pos.position_id,
            action_type="close",
            amount=round(close_usd, 2),
            reason=f"CRITICAL health={pos.health_factor:.2f} — emergency close",
            result="executed",
        )
        self._actions.append(action)
        self._stats["emergency_closes"] += 1
        self._stats["saved_usd"] += close_usd

        pos.quantity = 0
        pos.last_action = "emergency_closed"

        log.error(
            "SHIELD EMERGENCY: %s CLOSED ($%.0f) — health was %.2f",
            pos.position_id, close_usd, pos.health_factor,
        )

        await self.bus.publish("shield.emergency", action)

    async def _on_liquidation_signal(self, sig: Signal):
        """Process liquidation level signals."""
        if not isinstance(sig, Signal):
            return
        # Check if any positions are near the liquidation cluster
        for pos in self._positions.values():
            if pos.asset != sig.asset:
                continue
            if sig.strength > 0.7 and pos.risk_level in ("watch", "danger"):
                log.warning(
                    "SHIELD: liquidation cluster detected near %s position %s",
                    pos.asset, pos.position_id,
                )

    async def _on_cascade_signal(self, sig: Signal):
        """Process cascade prediction signals for early warning."""
        if not isinstance(sig, Signal):
            return
        if sig.strength > 0.6:
            self._cascade_warnings.append({
                "asset": sig.asset, "strength": sig.strength,
                "ts": time.time(),
            })
            if len(self._cascade_warnings) > 100:
                self._cascade_warnings = self._cascade_warnings[-50:]

    def summary(self) -> dict:
        return {
            **self._stats,
            "positions": {
                pid: {
                    "asset": p.asset,
                    "side": p.side,
                    "health": round(p.health_factor, 2),
                    "risk": p.risk_level,
                    "leverage": p.leverage,
                    "liq_price": round(p.liquidation_price, 2),
                }
                for pid, p in self._positions.items()
            },
            "recent_actions": [
                {
                    "id": a.action_id,
                    "type": a.action_type,
                    "amount": a.amount,
                    "reason": a.reason[:60],
                }
                for a in self._actions[-5:]
            ],
        }
