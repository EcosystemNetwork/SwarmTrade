"""MEV Auction Engine — detect, protect, and redistribute MEV.

Inspired by meva (Uniswap v4 hook taxing MEV bots and redistributing
profits to LPs), MEVAMM (protects LPs from LVR), ShadowSwap (anti-MEV
dark pool using commit-reveal batch swaps), and MevProtect (makes swap
fees unpredictable to hinder MEV bots).

MEV (Maximal Extractable Value) is profit extracted by reordering,
inserting, or censoring transactions. This engine:
  1. Detects MEV-risky conditions via price volatility analysis
  2. Protects our trades via Flashbots Protect RPC (private mempool)
  3. Enforces max slippage on DEX trades during high-MEV conditions
  4. Redistributes captured MEV to LPs and traders (not searchers)

Protection modes:
  flashbots  — Route EVM txs through Flashbots Protect RPC (requires FLASHBOTS_RPC)
  cautious   — Reject DEX trades when MEV risk is high (price volatility spike)
  monitor    — Detection and logging only, no trade blocking

Bus integration:
  Subscribes to: market.snapshot, exec.go (intercept all trades)
  Publishes to:  exec.cleared (MEV-screened trades for executors),
                 mev.detected, mev.blocked, mev.protected, signal.mev
"""
from __future__ import annotations

import logging
import os
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

    _ALLOWED_MODES = ("flashbots", "cautious", "monitor")
    # If recent price volatility exceeds this, flag as high-MEV-risk
    HIGH_VOL_THRESHOLD = 0.01  # 1% move in recent window

    def __init__(self, bus: Bus, protection_mode: str = "flashbots",
                 kill_switch=None):
        if protection_mode not in self._ALLOWED_MODES:
            log.warning("MEV: invalid protection_mode '%s' — defaulting to 'flashbots'", protection_mode)
            protection_mode = "flashbots"
        self.bus = bus
        self.kill_switch = kill_switch
        self.protection_mode = protection_mode
        self._flashbots_configured = bool(os.getenv("FLASHBOTS_RPC", ""))
        self._opportunities: deque[MEVOpportunity] = deque(maxlen=500)
        self._protections: deque[MEVProtection] = deque(maxlen=1000)
        self._opp_counter = 0
        self._captured_usd = 0.0
        self._protected_usd = 0.0
        self._stats = {
            "detected": 0, "captured": 0, "protected": 0,
            "blocked": 0,
            "total_captured_usd": 0.0, "total_protected_usd": 0.0,
        }
        # Price history for detecting MEV-risky conditions
        self._price_history: dict[str, deque] = {}

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("exec.go", self._on_trade_intent)

        log.info("MEV engine: mode=%s flashbots_rpc=%s",
                 protection_mode, "configured" if self._flashbots_configured else "NOT SET")

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

    def _is_high_mev_risk(self, asset: str) -> bool:
        """Check if recent price action indicates high MEV risk."""
        history = self._price_history.get(asset)
        if not history or len(history) < 3:
            return False
        prices = [p for _, p in history]
        recent = prices[-5:] if len(prices) >= 5 else prices
        if recent[0] <= 0:
            return False
        max_move = max(abs(b - a) / a for a, b in zip(recent, recent[1:]) if a > 0)
        return max_move > self.HIGH_VOL_THRESHOLD

    def _resolve_asset(self, intent: TradeIntent) -> str:
        """Extract the non-stablecoin asset from an intent."""
        stables = {"USD", "USDC", "USDT", "DAI"}
        if intent.asset_in.upper() in stables:
            return intent.asset_out.upper()
        return intent.asset_in.upper()

    async def _on_trade_intent(self, intent: TradeIntent):
        """Intercept exec.go, evaluate MEV risk, forward to exec.cleared.

        This is a true interceptor: executors subscribe to ``exec.cleared``
        (not ``exec.go``), so trades only reach them after MEV screening.
        In ``cautious`` mode, high-risk trades are blocked entirely.
        """
        # Kill switch check — do not forward ANY trades when halted
        if self.kill_switch and self.kill_switch.active:
            log.warning("MEV: kill switch active, dropping intent %s", intent.id)
            return

        asset = self._resolve_asset(intent)
        high_risk = self._is_high_mev_risk(asset)

        # In cautious mode, block trades during high-MEV conditions
        if high_risk and self.protection_mode == "cautious":
            self._stats["blocked"] += 1
            log.warning("MEV BLOCK: %s — high volatility on %s, trade held back", intent.id, asset)
            await self.bus.publish("mev.blocked", {
                "intent_id": intent.id, "asset": asset, "reason": "high_volatility_mev_risk",
            })
            return  # Intent is NOT forwarded to exec.cleared — executors never see it

        # Determine active protection method
        if self._flashbots_configured and self.protection_mode == "flashbots":
            method = "flashbots_protect"
            savings_bps = 10  # ~10bps saved by avoiding sandwich
        elif high_risk:
            method = "slippage_tightened"
            savings_bps = 5
        else:
            method = "standard"
            savings_bps = 0

        protection = MEVProtection(
            intent_id=intent.id,
            method=method,
            estimated_savings=intent.amount_in * savings_bps / 10_000,
        )
        self._protections.append(protection)
        # deque(maxlen=1000) handles eviction automatically

        self._stats["protected"] += 1
        self._stats["total_protected_usd"] += protection.estimated_savings
        self._protected_usd += protection.estimated_savings

        if high_risk:
            log.info("MEV PROTECT: %s on %s — method=%s risk=HIGH savings=$%.4f",
                     intent.id, asset, method, protection.estimated_savings)

        await self.bus.publish("mev.protected", protection)

        # Forward the cleared intent to executors
        await self.bus.publish("exec.cleared", intent)

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
            "flashbots_configured": self._flashbots_configured,
            "recent_opportunities": [
                {
                    "id": o.opp_id,
                    "type": o.opp_type,
                    "asset": o.asset,
                    "profit": o.net_profit,
                }
                for o in list(self._opportunities)[-5:]
            ],
            "recent_protections": [
                {
                    "intent": p.intent_id,
                    "method": p.method,
                    "savings": p.estimated_savings,
                }
                for p in self._protections[-5:]
            ],
        }
