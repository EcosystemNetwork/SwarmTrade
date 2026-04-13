"""x402 Agent Payment Rails — HTTP-native USDC micropayments between agents.

Inspired by Synapse (ETHGlobal Cannes 2026). Enables agent-to-agent commerce
where external agents can pay for signals/data from our swarm, and our agents
can pay for external intelligence.

Protocol: x402 (Circle's HTTP-native payment standard)
  - Client sends payment header with USDC authorization
  - Server validates payment, delivers content
  - Micropayments as low as $0.001

Settlement chains: Stellar (primary, sub-cent fees), Base (secondary), Ethereum (fallback)

The x402 protocol on Stellar uses Soroban authorization and Stellar's native
USDC for near-instant settlement at negligible cost, making high-frequency
agent micropayments economically viable.

Environment variables:
  X402_PRIVATE_KEY       — wallet key for receiving/sending payments (EVM)
  STELLAR_SECRET_KEY     — Stellar secret key for Stellar settlement
  X402_USDC_ADDRESS      — USDC contract (default: Base USDC)
  X402_MIN_BALANCE       — minimum USDC balance to maintain
  X402_FACILITATOR       — payment facilitator address (optional)
  X402_SETTLEMENT_CHAIN  — preferred settlement: "stellar", "base", "ethereum"
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .core import Bus, Signal

log = logging.getLogger("swarm.x402")

# USDC contract addresses
USDC_CONTRACTS = {
    8453: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",   # Base
    1: "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",       # Ethereum
    42161: "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",    # Arbitrum
}


@dataclass
class PaymentReceipt:
    """Record of a completed micropayment."""
    tx_hash: str
    from_agent: str
    to_agent: str
    amount_usdc: float
    service: str  # what was paid for
    timestamp: float = field(default_factory=time.time)
    chain_id: int = 8453
    status: str = "confirmed"  # "pending", "confirmed", "failed"


@dataclass
class PricingTier:
    """Pricing for a service offered by an agent."""
    service_id: str
    description: str
    price_usdc: float  # per request
    rate_limit: int = 100  # max requests per hour
    requires_verification: bool = False


@dataclass
class AgentAccount:
    """Financial account for an agent in the payment system."""
    agent_id: str
    address: str
    balance_usdc: float = 0.0
    total_earned: float = 0.0
    total_spent: float = 0.0
    transaction_count: int = 0


class PaymentLedger:
    """In-memory ledger tracking agent-to-agent payments.

    For production, this would be backed by the database + on-chain verification.
    The ledger tracks:
      - Per-agent balances (earned - spent)
      - Payment history
      - Service pricing
      - Rate limiting per consumer
    """

    def __init__(self):
        self.accounts: dict[str, AgentAccount] = {}
        self.receipts: list[PaymentReceipt] = []
        self.services: dict[str, PricingTier] = {}
        self._rate_counts: dict[str, list[float]] = {}  # agent_id -> [timestamps]
        self._max_receipts = 10000

    def register_account(self, agent_id: str, address: str) -> AgentAccount:
        if agent_id not in self.accounts:
            self.accounts[agent_id] = AgentAccount(agent_id=agent_id, address=address)
        return self.accounts[agent_id]

    def register_service(self, tier: PricingTier):
        self.services[tier.service_id] = tier

    def can_afford(self, agent_id: str, amount: float) -> bool:
        acct = self.accounts.get(agent_id)
        return acct is not None and acct.balance_usdc >= amount

    def check_rate_limit(self, agent_id: str, service_id: str) -> bool:
        tier = self.services.get(service_id)
        if not tier:
            return False
        key = f"{agent_id}:{service_id}"
        now = time.time()
        timestamps = self._rate_counts.get(key, [])
        # Prune old timestamps (> 1 hour)
        timestamps = [t for t in timestamps if now - t < 3600]
        self._rate_counts[key] = timestamps
        return len(timestamps) < tier.rate_limit

    def record_payment(
        self, from_agent: str, to_agent: str,
        amount: float, service: str, tx_hash: str = "",
    ) -> PaymentReceipt | None:
        sender = self.accounts.get(from_agent)
        receiver = self.accounts.get(to_agent)
        if not sender or not receiver:
            return None
        if sender.balance_usdc < amount:
            return None

        sender.balance_usdc -= amount
        sender.total_spent += amount
        sender.transaction_count += 1

        receiver.balance_usdc += amount
        receiver.total_earned += amount
        receiver.transaction_count += 1

        receipt = PaymentReceipt(
            tx_hash=tx_hash or hashlib.sha256(
                f"{from_agent}:{to_agent}:{amount}:{time.time()}".encode()
            ).hexdigest()[:16],
            from_agent=from_agent,
            to_agent=to_agent,
            amount_usdc=amount,
            service=service,
        )
        self.receipts.append(receipt)
        if len(self.receipts) > self._max_receipts:
            self.receipts = self.receipts[-self._max_receipts // 2:]

        # Record rate limit
        key = f"{from_agent}:{service}"
        self._rate_counts.setdefault(key, []).append(time.time())

        return receipt

    def summary(self) -> dict:
        return {
            "total_accounts": len(self.accounts),
            "total_services": len(self.services),
            "total_payments": len(self.receipts),
            "total_volume_usdc": sum(r.amount_usdc for r in self.receipts),
            "accounts": {
                aid: {
                    "balance": round(a.balance_usdc, 6),
                    "earned": round(a.total_earned, 6),
                    "spent": round(a.total_spent, 6),
                    "txns": a.transaction_count,
                }
                for aid, a in self.accounts.items()
            },
        }


# Default services the swarm offers
DEFAULT_SERVICES = [
    PricingTier("signal.realtime", "Real-time trading signals", 0.01, rate_limit=60),
    PricingTier("signal.smart_money", "Smart money wallet tracking", 0.02, rate_limit=30),
    PricingTier("signal.confluence", "Multi-indicator confluence signals", 0.015, rate_limit=30),
    PricingTier("data.market_snapshot", "Current market snapshot", 0.001, rate_limit=300),
    PricingTier("data.portfolio", "Portfolio state snapshot", 0.005, rate_limit=60),
    PricingTier("data.orderbook", "Order book analysis", 0.005, rate_limit=60),
    PricingTier("analysis.var", "Value-at-Risk computation", 0.05, rate_limit=10),
    PricingTier("analysis.stress_test", "Stress test scenario", 0.10, rate_limit=5),
    PricingTier("execution.quote", "Smart order routing quote", 0.005, rate_limit=60),
    PricingTier("intelligence.alpha", "Alpha signal package", 0.10, rate_limit=10),
]


class X402PaymentGateway:
    """Payment gateway for agent-to-agent commerce via x402 protocol.

    Integrates with the AgentGateway to:
    1. Accept payments from external agents for our signals/data
    2. Pay external agents for their intelligence
    3. Track revenue and expenses per agent
    4. Settle on-chain via USDC on Stellar (primary) or Base (fallback)

    HTTP headers used:
      X-402-Payment: <signed USDC authorization>
      X-402-Amount: <amount in USDC>
      X-402-Service: <service_id being requested>
      X-402-Chain: <settlement chain: "stellar", "base", "ethereum">
    """

    name = "x402_payments"

    def __init__(
        self,
        bus: Bus,
        private_key: str = "",
        chain_id: int = 8453,
        deposit_amount: float = 100.0,
        settlement_chain: str = "",
    ):
        self.bus = bus
        self._private_key = private_key or os.getenv("X402_PRIVATE_KEY", "")
        self._chain_id = chain_id
        self._usdc_address = USDC_CONTRACTS.get(chain_id, USDC_CONTRACTS[8453])
        self._settlement_chain = settlement_chain or os.getenv(
            "X402_SETTLEMENT_CHAIN", "stellar"
        )
        self.ledger = PaymentLedger()
        self._deposit_amount = deposit_amount
        self._address = ""
        self._stop = False
        self._stellar_gateway = None  # set via link_stellar()

        # Register default services
        for svc in DEFAULT_SERVICES:
            self.ledger.register_service(svc)

        # Register swarm's own account
        self.ledger.register_account("swarm_treasury", "0x0000")
        self.ledger.accounts["swarm_treasury"].balance_usdc = deposit_amount

        # Subscribe to events
        bus.subscribe("gateway.agent_connected", self._on_agent_connected)
        bus.subscribe("x402.payment_request", self._on_payment_request)

    async def start(self):
        """Initialize wallet and verify on-chain USDC balance."""
        if self._private_key:
            try:
                from .thirdweb_wallet import ThirdwebWallet
                self._wallet = ThirdwebWallet(
                    private_key=self._private_key, chain_id=self._chain_id,
                )
                self._address = self._wallet.address
                self.ledger.accounts["swarm_treasury"].address = self._address
                log.info("x402 payment gateway: address=%s chain=%d (thirdweb)",
                         self._address, self._chain_id)
            except Exception as e:
                log.warning("x402 wallet init failed (running in simulation mode): %s", e)

    async def _on_agent_connected(self, msg: dict):
        """When an external agent connects, create a payment account for it."""
        agent_id = msg.get("agent_id", "")
        address = msg.get("address", "0x0000")
        if agent_id:
            self.ledger.register_account(agent_id, address)
            log.info("x402: registered payment account for agent %s", agent_id)

    async def _on_payment_request(self, msg: dict):
        """Process an incoming payment request from an external agent."""
        from_agent = msg.get("from_agent", "")
        service_id = msg.get("service", "")
        payment_proof = msg.get("payment_proof", "")

        tier = self.ledger.services.get(service_id)
        if not tier:
            await self.bus.publish("x402.payment_rejected", {
                "from_agent": from_agent, "reason": f"unknown service: {service_id}",
            })
            return

        if not self.ledger.check_rate_limit(from_agent, service_id):
            await self.bus.publish("x402.payment_rejected", {
                "from_agent": from_agent, "reason": "rate limit exceeded",
            })
            return

        # In production, verify payment_proof against on-chain USDC transfer
        # For now, debit from the agent's internal balance
        receipt = self.ledger.record_payment(
            from_agent=from_agent,
            to_agent="swarm_treasury",
            amount=tier.price_usdc,
            service=service_id,
            tx_hash=payment_proof,
        )

        if receipt:
            await self.bus.publish("x402.payment_confirmed", {
                "receipt": receipt.__dict__,
                "service": service_id,
                "from_agent": from_agent,
            })
            log.info("x402: payment confirmed $%.4f from %s for %s",
                     tier.price_usdc, from_agent, service_id)
        else:
            await self.bus.publish("x402.payment_rejected", {
                "from_agent": from_agent,
                "reason": "insufficient balance or account not found",
            })

    async def pay_external_agent(
        self, to_agent: str, service: str, amount: float
    ) -> PaymentReceipt | None:
        """Pay an external agent for their intelligence/service."""
        receipt = self.ledger.record_payment(
            from_agent="swarm_treasury",
            to_agent=to_agent,
            amount=amount,
            service=service,
        )
        if receipt:
            log.info("x402: paid $%.4f to %s for %s", amount, to_agent, service)
            await self.bus.publish("x402.payment_sent", {"receipt": receipt.__dict__})
        return receipt

    def get_service_catalog(self) -> list[dict]:
        """Return the service catalog for external agents."""
        return [
            {
                "service_id": t.service_id,
                "description": t.description,
                "price_usdc": t.price_usdc,
                "rate_limit_hourly": t.rate_limit,
            }
            for t in self.ledger.services.values()
        ]

    def link_stellar(self, stellar_gateway):
        """Link the Stellar payment gateway for on-chain settlement."""
        self._stellar_gateway = stellar_gateway
        log.info("x402: linked Stellar gateway for settlement")

    async def settle_on_stellar(
        self, to_agent: str, amount: float, service: str
    ) -> str:
        """Settle a payment on Stellar instead of EVM."""
        if not self._stellar_gateway:
            log.warning("x402: Stellar gateway not linked, cannot settle")
            return ""
        receipt = await self._stellar_gateway.pay(
            from_agent="swarm_treasury",
            to_agent=to_agent,
            amount=amount,
            service=service,
            settle_on_chain=True,
        )
        return receipt.tx_hash if receipt else ""

    def status(self) -> dict:
        return {
            "address": self._address,
            "chain_id": self._chain_id,
            "settlement_chain": self._settlement_chain,
            "stellar_linked": self._stellar_gateway is not None,
            "usdc_contract": self._usdc_address,
            "services_offered": len(self.ledger.services),
            "ledger": self.ledger.summary(),
        }
