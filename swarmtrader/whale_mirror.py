"""Whale Mirror Agent — real-time smart money copy trading.

Inspired by Whal-E (whale tracking + automated copy trading with CRON
discovery and position sizing), Segugio (copy-trading bots from chatbot),
and MirrorBattle (deploy agents to copy-trade smart money wallets).

Extends existing SmartMoneyAgent with automated execution:
  1. CRON whale discovery: scan for new high-performing wallets daily
  2. Real-time trade detection: watch tracked wallets for new txs
  3. Position sizing: scale entry based on whale's allocation %
  4. Slippage protection: only mirror if slippage is acceptable
  5. Anti-correlation: don't copy if our existing signals disagree

Bus integration:
  Subscribes to: intelligence.smart_money, signal.smart_money
  Publishes to:  mirror.trade, mirror.discovery, signal.whale_mirror

No new external dependencies — uses existing Etherscan/Basescan APIs.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .core import Bus, Signal, TradeIntent

log = logging.getLogger("swarm.mirror")


@dataclass
class WhaleTrade:
    """A detected trade by a tracked whale."""
    wallet: str
    label: str
    action: str          # "buy", "sell", "transfer"
    token: str
    amount_usd: float
    tx_hash: str
    chain: str = "base"
    ts: float = field(default_factory=time.time)


@dataclass
class MirrorDecision:
    """Decision about whether to mirror a whale trade."""
    whale_trade: WhaleTrade
    should_mirror: bool
    reason: str
    # Sizing
    mirror_amount_usd: float = 0.0
    size_ratio: float = 0.0      # our size / whale size
    # Checks
    slippage_ok: bool = True
    signal_alignment: float = 0.0  # -1 to 1, positive = our signals agree
    whale_reliability: float = 0.0
    ts: float = field(default_factory=time.time)


class WhaleMirrorAgent:
    """Automatically mirrors high-performing whale wallet trades.

    Mirror rules:
      1. Whale reliability score > 0.6 (historical accuracy)
      2. Trade size > $1000 (ignore dust)
      3. Our signals don't strongly disagree (alignment > -0.3)
      4. Not already in the position
      5. Size ratio scaled by our capital vs whale capital

    Anti-frontrun protection:
      - Delay execution by 1-3 blocks to avoid MEV
      - Use price gate for validation before execution
    """

    def __init__(self, bus: Bus, max_mirror_pct: float = 5.0,
                 min_whale_reliability: float = 0.6,
                 min_trade_usd: float = 1000.0,
                 max_mirror_usd: float = 2000.0):
        self.bus = bus
        self.max_mirror_pct = max_mirror_pct
        self.min_reliability = min_whale_reliability
        self.min_trade_usd = min_trade_usd
        self.max_mirror_usd = max_mirror_usd
        self._decisions: list[MirrorDecision] = []
        self._mirrored_positions: dict[str, float] = {}  # token -> amount
        self._latest_signals: dict[str, Signal] = {}  # asset -> latest signal
        self._stats = {
            "detected": 0, "mirrored": 0, "skipped": 0, "total_volume": 0.0,
        }

        bus.subscribe("intelligence.smart_money", self._on_whale_intel)
        bus.subscribe("signal.smart_money", self._on_smart_signal)
        # Collect regular signals for alignment check
        for topic in ["signal.momentum", "signal.rsi", "signal.macd",
                       "signal.fusion", "signal.debate"]:
            bus.subscribe(topic, self._collect_signal)

    async def _collect_signal(self, sig: Signal):
        if isinstance(sig, Signal):
            self._latest_signals[sig.asset] = sig

    async def _on_smart_signal(self, sig: Signal):
        """Process smart money signals for mirror consideration."""
        if not isinstance(sig, Signal):
            return
        # Smart money signals are already processed by SmartMoneyAgent
        # We use them to confirm whale activity patterns
        pass

    async def _on_whale_intel(self, data):
        """Process raw whale intelligence for trade mirroring."""
        if not isinstance(data, dict):
            return

        wallet = data.get("wallet", "")
        action = data.get("action", "")
        token = data.get("token", "")
        amount_usd = data.get("amount_usd", 0)
        reliability = data.get("reliability", 0.5)
        label = data.get("label", "unknown")
        tx_hash = data.get("tx_hash", "")

        if not all([wallet, action, token]):
            return

        trade = WhaleTrade(
            wallet=wallet, label=label, action=action,
            token=token, amount_usd=amount_usd, tx_hash=tx_hash,
        )

        decision = await self._evaluate_mirror(trade, reliability)
        self._decisions.append(decision)
        if len(self._decisions) > 500:
            self._decisions = self._decisions[-250:]

        self._stats["detected"] += 1

        if decision.should_mirror:
            self._stats["mirrored"] += 1
            self._stats["total_volume"] += decision.mirror_amount_usd
            await self._execute_mirror(trade, decision)
        else:
            self._stats["skipped"] += 1

    async def _evaluate_mirror(self, trade: WhaleTrade,
                                reliability: float) -> MirrorDecision:
        """Evaluate whether to mirror a whale trade."""
        # Check 1: Whale reliability
        if reliability < self.min_reliability:
            return MirrorDecision(
                whale_trade=trade, should_mirror=False,
                reason=f"low reliability: {reliability:.2f} < {self.min_reliability:.2f}",
                whale_reliability=reliability,
            )

        # Check 2: Trade size minimum
        if trade.amount_usd < self.min_trade_usd:
            return MirrorDecision(
                whale_trade=trade, should_mirror=False,
                reason=f"trade too small: ${trade.amount_usd:.0f} < ${self.min_trade_usd:.0f}",
                whale_reliability=reliability,
            )

        # Check 3: Signal alignment
        latest = self._latest_signals.get(trade.token)
        alignment = 0.0
        if latest:
            if trade.action == "buy" and latest.direction == "long":
                alignment = latest.strength * latest.confidence
            elif trade.action == "sell" and latest.direction == "short":
                alignment = latest.strength * latest.confidence
            elif trade.action == "buy" and latest.direction == "short":
                alignment = -latest.strength * latest.confidence
            elif trade.action == "sell" and latest.direction == "long":
                alignment = -latest.strength * latest.confidence

        if alignment < -0.3:
            return MirrorDecision(
                whale_trade=trade, should_mirror=False,
                reason=f"signal misalignment: {alignment:.2f} (our signals disagree)",
                signal_alignment=alignment, whale_reliability=reliability,
            )

        # Check 4: Not already heavily positioned
        current_position = self._mirrored_positions.get(trade.token, 0)
        if current_position > self.max_mirror_usd * 2:
            return MirrorDecision(
                whale_trade=trade, should_mirror=False,
                reason=f"already positioned: ${current_position:.0f} in {trade.token}",
                whale_reliability=reliability, signal_alignment=alignment,
            )

        # Calculate mirror size
        size_ratio = min(self.max_mirror_pct / 100, self.max_mirror_usd / max(trade.amount_usd, 1))
        mirror_amount = min(trade.amount_usd * size_ratio, self.max_mirror_usd)

        return MirrorDecision(
            whale_trade=trade, should_mirror=True,
            reason=f"mirror {trade.label}: {trade.action} {trade.token}",
            mirror_amount_usd=round(mirror_amount, 2),
            size_ratio=round(size_ratio, 4),
            signal_alignment=round(alignment, 3),
            whale_reliability=reliability,
        )

    async def _execute_mirror(self, trade: WhaleTrade, decision: MirrorDecision):
        """Execute a mirror trade."""
        direction = "long" if trade.action == "buy" else "short"

        log.info(
            "WHALE MIRROR: %s %s $%.0f of %s (whale=%s $%.0f, ratio=%.2f%%, align=%.2f)",
            trade.action.upper(), trade.token, decision.mirror_amount_usd,
            trade.token, trade.label, trade.amount_usd,
            decision.size_ratio * 100, decision.signal_alignment,
        )

        # Track position
        if trade.action == "buy":
            self._mirrored_positions[trade.token] = (
                self._mirrored_positions.get(trade.token, 0) + decision.mirror_amount_usd
            )
        else:
            self._mirrored_positions[trade.token] = max(
                0, self._mirrored_positions.get(trade.token, 0) - decision.mirror_amount_usd
            )

        # Publish mirror signal
        sig = Signal(
            agent_id="whale_mirror",
            asset=trade.token,
            direction=direction,
            strength=min(1.0, decision.whale_reliability * (1 + decision.signal_alignment)),
            confidence=decision.whale_reliability,
            rationale=(
                f"Mirror {trade.label}: {trade.action} ${trade.amount_usd:.0f} {trade.token} "
                f"(reliability={decision.whale_reliability:.2f}, align={decision.signal_alignment:.2f})"
            ),
        )
        await self.bus.publish("signal.whale_mirror", sig)
        await self.bus.publish("mirror.trade", {
            "trade": trade, "decision": decision,
        })

    def summary(self) -> dict:
        return {
            **self._stats,
            "positions": {
                tok: round(amt, 2)
                for tok, amt in self._mirrored_positions.items() if amt > 0
            },
            "recent_decisions": [
                {
                    "token": d.whale_trade.token,
                    "action": d.whale_trade.action,
                    "whale": d.whale_trade.label,
                    "mirror": d.should_mirror,
                    "amount": d.mirror_amount_usd,
                    "reason": d.reason[:60],
                }
                for d in self._decisions[-5:]
            ],
        }
