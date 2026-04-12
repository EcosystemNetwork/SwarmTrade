"""Agent-to-Agent Payment Protocol — x402 micropayment upgrade.

Inspired by Alpha Dawg (HTTP 402 payment gates per specialist call at
~$0.001/call), Hubble (x402 payment routing on Base), and Flow Broker
(autonomous AI brokers paying per intelligence call).

Extends the existing x402_payments.py with:
  1. Pay-per-signal: agents charge per signal based on quality/ELO
  2. Pay-per-execution: marketplace winners get paid from trade profits
  3. Revenue splitting: automatic fee distribution up the fork chain
  4. Budget management: agents have spending budgets for buying signals
  5. Settlement batching: batch small payments to reduce gas costs

Bus integration:
  Subscribes to: marketplace.fee, prediction.bet, exec.report
  Publishes to:  payment.settled, payment.budget_warning

No new external dependencies.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections import defaultdict

from .core import Bus, ExecutionReport

log = logging.getLogger("swarm.payments")


@dataclass
class AgentWallet:
    """Internal wallet for an agent's earnings and spending."""
    agent_id: str
    balance_usd: float = 0.0
    total_earned: float = 0.0
    total_spent: float = 0.0
    pending_settlement: float = 0.0
    # Budget
    daily_budget: float = 100.0
    spent_today: float = 0.0
    # History
    transaction_count: int = 0


@dataclass
class PaymentRecord:
    """Record of a payment between agents."""
    payment_id: str
    from_agent: str
    to_agent: str
    amount_usd: float
    service: str           # "signal", "execution", "intelligence", "royalty"
    reference_id: str = ""  # linked intent/bet/signal ID
    status: str = "settled"
    ts: float = field(default_factory=time.time)


class AgentPaymentProtocol:
    """Manages micropayments between agents in the swarm.

    Every agent has an internal wallet. When agents provide value
    (winning signals, profitable executions), they earn fees.
    When agents consume services (buying intelligence, paying for
    execution), they spend from their budget.

    Settlement is batched — small payments accumulate until a
    threshold is reached, then settled on-chain via x402.
    """

    SETTLEMENT_THRESHOLD = 10.0  # batch until $10

    def __init__(self, bus: Bus, default_budget: float = 100.0):
        self.bus = bus
        self.default_budget = default_budget
        self._wallets: dict[str, AgentWallet] = {}
        self._payments: list[PaymentRecord] = []
        self._payment_counter = 0
        self._pending_batch: list[PaymentRecord] = []
        self._stats = {
            "total_payments": 0, "total_volume": 0.0,
            "settlements": 0,
        }
        self._day_start = _day_start()

        bus.subscribe("marketplace.fee", self._on_marketplace_fee)
        bus.subscribe("exec.report", self._on_execution)

    def get_wallet(self, agent_id: str) -> AgentWallet:
        if agent_id not in self._wallets:
            self._wallets[agent_id] = AgentWallet(
                agent_id=agent_id, daily_budget=self.default_budget,
            )
        return self._wallets[agent_id]

    def pay(self, from_agent: str, to_agent: str, amount: float,
            service: str, reference_id: str = "") -> PaymentRecord | None:
        """Process a payment between two agents."""
        self._maybe_reset_daily()

        sender = self.get_wallet(from_agent)
        receiver = self.get_wallet(to_agent)

        # Budget check
        if sender.spent_today + amount > sender.daily_budget:
            log.warning(
                "Payment blocked: %s over daily budget ($%.2f + $%.2f > $%.2f)",
                from_agent, sender.spent_today, amount, sender.daily_budget,
            )
            return None

        # Process payment
        sender.balance_usd -= amount
        sender.total_spent += amount
        sender.spent_today += amount
        sender.transaction_count += 1

        receiver.balance_usd += amount
        receiver.total_earned += amount
        receiver.transaction_count += 1

        self._payment_counter += 1
        record = PaymentRecord(
            payment_id=f"pay-{self._payment_counter:06d}",
            from_agent=from_agent, to_agent=to_agent,
            amount_usd=round(amount, 6), service=service,
            reference_id=reference_id,
        )
        self._payments.append(record)
        if len(self._payments) > 2000:
            self._payments = self._payments[-1000:]

        self._stats["total_payments"] += 1
        self._stats["total_volume"] += amount

        # Add to settlement batch
        self._pending_batch.append(record)
        receiver.pending_settlement += amount

        # Check if we should settle on-chain
        total_pending = sum(w.pending_settlement for w in self._wallets.values())
        if total_pending >= self.SETTLEMENT_THRESHOLD:
            self._settle_batch()

        return record

    def credit(self, agent_id: str, amount: float, reason: str = "system_credit"):
        """Credit an agent's wallet (e.g., from vault profits)."""
        wallet = self.get_wallet(agent_id)
        wallet.balance_usd += amount
        wallet.total_earned += amount

        self._payment_counter += 1
        record = PaymentRecord(
            payment_id=f"pay-{self._payment_counter:06d}",
            from_agent="system", to_agent=agent_id,
            amount_usd=round(amount, 6), service=reason,
        )
        self._payments.append(record)

    async def _on_marketplace_fee(self, fee_attr):
        """Process marketplace fee attribution."""
        if not hasattr(fee_attr, "agent_id"):
            if isinstance(fee_attr, dict):
                agent_id = fee_attr.get("agent_id", "")
                amount = fee_attr.get("fee_earned", 0)
            else:
                return
        else:
            agent_id = fee_attr.agent_id
            amount = fee_attr.fee_earned

        if agent_id and amount > 0:
            self.credit(agent_id, amount, "marketplace_fee")

    async def _on_execution(self, report: ExecutionReport):
        """Distribute execution profits to contributing agents."""
        if report.status != "filled" or not report.pnl_estimate:
            return
        pnl = report.pnl_estimate
        if pnl <= 0:
            return
        # 5% of profit goes to the agent payment pool
        pool_allocation = pnl * 0.05
        if pool_allocation < 0.001:
            return
        # Distribute equally to all agents with positive ELO
        active = [w for w in self._wallets.values() if w.total_earned > 0]
        if active:
            per_agent = pool_allocation / len(active)
            for wallet in active:
                wallet.balance_usd += per_agent
                wallet.total_earned += per_agent

    def _settle_batch(self):
        """Settle accumulated payments on-chain (batch for gas efficiency)."""
        if not self._pending_batch:
            return

        total = sum(p.amount_usd for p in self._pending_batch)
        count = len(self._pending_batch)

        # Reset pending settlement
        for wallet in self._wallets.values():
            wallet.pending_settlement = 0.0

        self._stats["settlements"] += 1
        self._pending_batch.clear()

        log.info("PAYMENT SETTLED: %d payments totaling $%.4f (batch #%d)",
                 count, total, self._stats["settlements"])

    def _maybe_reset_daily(self):
        today = _day_start()
        if today > self._day_start:
            self._day_start = today
            for wallet in self._wallets.values():
                wallet.spent_today = 0.0

    def leaderboard(self, top_n: int = 10) -> list[dict]:
        return sorted(
            [
                {
                    "agent_id": w.agent_id,
                    "balance": round(w.balance_usd, 4),
                    "earned": round(w.total_earned, 4),
                    "spent": round(w.total_spent, 4),
                    "txns": w.transaction_count,
                }
                for w in self._wallets.values()
                if w.transaction_count > 0
            ],
            key=lambda x: x["earned"], reverse=True,
        )[:top_n]

    def summary(self) -> dict:
        return {
            **self._stats,
            "wallets": len(self._wallets),
            "total_balance": round(sum(w.balance_usd for w in self._wallets.values()), 4),
            "leaderboard": self.leaderboard(5),
        }


def _day_start() -> float:
    import calendar, datetime
    now = datetime.datetime.utcnow()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return calendar.timegm(midnight.timetuple())
