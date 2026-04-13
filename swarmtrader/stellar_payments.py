"""Stellar Payment Rails — x402 + MPP agent micropayments on Stellar.

Implements the x402 protocol (Coinbase/Linux Foundation) and MPP (Machine
Payments Protocol by Stripe/Tempo) on Stellar for agent-to-agent commerce.

x402 Protocol Flow (HTTP 402):
  1. Client requests a paid endpoint (e.g. GET /api/signal/realtime)
  2. Server returns HTTP 402 with challenge:
     - X-Payment-Required: {"amount": "10000", "asset": "<USDC_SAC>",
       "recipient": "G...", "network": "stellar:testnet", "scheme": "exact-v2"}
  3. Client builds Soroban SAC transfer auth entry, signs it
  4. Client retries with Authorization header containing signed payment
  5. Facilitator verifies + settles on-chain, server returns content

MPP Protocol Flow (Machine Payments Protocol):
  - Charge Intent: one-time Soroban SAC transfer per request
  - Session Intent: payment channel with cumulative off-chain commitments
  - Pull mode: client signs auth entries, server broadcasts tx
  - Push mode: client broadcasts tx, sends hash for verification

References:
  - x402 spec: https://developers.stellar.org/docs/build/agentic-payments/x402
  - MPP spec: https://mpp.dev/overview
  - Built on Stellar facilitator: @x402/stellar npm, OpenZeppelin Relayer
  - Soroban USDC SAC: Stellar Asset Contract wrappers for native USDC
  - Facilitator endpoints: /verify, /settle, /supported

Environment variables:
  STELLAR_SECRET_KEY       — Stellar secret key (starts with S...)
  STELLAR_NETWORK          — "testnet" or "public" (default: testnet)
  STELLAR_USDC_ISSUER      — USDC issuer on Stellar (Circle's address)
  STELLAR_HORIZON_URL      — Horizon API endpoint (auto-set from network)
  STELLAR_FACILITATOR_URL  — x402 facilitator (default: OpenZeppelin testnet)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

from .core import Bus, Signal

log = logging.getLogger("swarm.stellar")

# Stellar network configuration
STELLAR_NETWORKS = {
    "testnet": {
        "horizon": "https://horizon-testnet.stellar.org",
        "passphrase": "Test SDF Network ; September 2015",
        "usdc_issuer": "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5",
    },
    "public": {
        "horizon": "https://horizon.stellar.org",
        "passphrase": "Public Global Stellar Network ; September 2015",
        "usdc_issuer": "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
    },
}

# Soroban USDC contract IDs (Stellar Asset Contract wrappers)
SOROBAN_USDC = {
    "testnet": "CBIELTK6YBZJU5UP2WWQEUCYKLPU6AUNZ2BQ4WWFEIE3USCIHMXQDAMA",
    "public": "CCW67TSZV3SSS2HXMBQ5JFGCKJNXKZM7UQUWUZPUTHXSTZLEO7SJMI75",
}


@dataclass
class StellarPaymentReceipt:
    """Record of a completed Stellar micropayment."""
    tx_hash: str
    from_agent: str
    to_agent: str
    amount_usdc: float
    service: str
    timestamp: float = field(default_factory=time.time)
    network: str = "testnet"
    status: str = "confirmed"
    stellar_from: str = ""  # Stellar public key of sender
    stellar_to: str = ""    # Stellar public key of receiver
    ledger: int = 0         # Stellar ledger number


@dataclass
class StellarAgentAccount:
    """Stellar-native agent account with keypair and USDC trustline."""
    agent_id: str
    public_key: str
    balance_xlm: float = 0.0
    balance_usdc: float = 0.0
    total_earned: float = 0.0
    total_spent: float = 0.0
    transaction_count: int = 0
    has_usdc_trustline: bool = False


class StellarPaymentGateway:
    """Payment gateway for agent-to-agent commerce on Stellar.

    Extends the x402 payment protocol to Stellar, enabling:
    1. USDC micropayments between agents via Stellar transactions
    2. XLM-denominated payments for ultra-low-value operations
    3. Soroban contract calls for programmable spending policies
    4. Stellar DEX integration for on-chain token swaps
    5. Multi-signature agent wallets with spending limits

    Settlement on Stellar is near-instant (~5s) with fees < $0.001,
    making it ideal for high-frequency agent micropayments.
    """

    name = "stellar_payments"

    def __init__(
        self,
        bus: Bus,
        secret_key: str = "",
        network: str = "testnet",
        deposit_amount: float = 100.0,
    ):
        self.bus = bus
        self._secret_key = secret_key or os.getenv("STELLAR_SECRET_KEY", "")
        self._network = network if network in STELLAR_NETWORKS else "testnet"
        self._net_cfg = STELLAR_NETWORKS[self._network]
        self._horizon_url = os.getenv("STELLAR_HORIZON_URL", self._net_cfg["horizon"])
        self._usdc_issuer = os.getenv("STELLAR_USDC_ISSUER", self._net_cfg["usdc_issuer"])
        self._soroban_usdc = SOROBAN_USDC.get(self._network, "")
        self._deposit_amount = deposit_amount

        self._public_key = ""
        self._keypair = None
        self._server = None
        self._stop = False

        # Internal ledger (mirrors on-chain state)
        self.accounts: dict[str, StellarAgentAccount] = {}
        self.receipts: list[StellarPaymentReceipt] = []
        self.services: dict[str, dict] = {}
        self._rate_counts: dict[str, list[float]] = {}
        self._max_receipts = 10000

        # Stats
        self._stats = {
            "total_payments": 0,
            "total_volume_usdc": 0.0,
            "total_volume_xlm": 0.0,
            "on_chain_settlements": 0,
            "failed_settlements": 0,
        }

        # Subscribe to bus events
        bus.subscribe("gateway.agent_connected", self._on_agent_connected)
        bus.subscribe("stellar.payment_request", self._on_payment_request)
        bus.subscribe("stellar.fund_agent", self._on_fund_agent)

    async def start(self):
        """Initialize Stellar keypair and verify account on network."""
        if not self._secret_key:
            log.warning("Stellar: no secret key — running in simulation mode")
            self._public_key = "GSIMULATION000000000000000000000000000000000000000000000"
            self._register_treasury()
            return

        try:
            from stellar_sdk import Keypair, Server
            from stellar_sdk.network import Network

            kp = Keypair.from_secret(self._secret_key)
            self._keypair = kp
            self._public_key = kp.public_key
            self._server = Server(self._horizon_url)

            # Check if account exists on network
            try:
                account = self._server.accounts().account_id(self._public_key).call()
                balances = account.get("balances", [])
                for bal in balances:
                    if bal.get("asset_type") == "native":
                        xlm_balance = float(bal.get("balance", "0"))
                        log.info("Stellar account: %s XLM=%.4f",
                                 self._public_key[:12] + "...", xlm_balance)
                    elif (bal.get("asset_code") == "USDC" and
                          bal.get("asset_issuer") == self._usdc_issuer):
                        usdc_balance = float(bal.get("balance", "0"))
                        log.info("Stellar USDC balance: %.4f", usdc_balance)

            except Exception as e:
                log.warning("Stellar account not found (need funding): %s", e)
                if self._network == "testnet":
                    await self._fund_testnet_account()

            self._register_treasury()
            log.info("Stellar payment gateway: network=%s address=%s horizon=%s",
                     self._network, self._public_key[:12] + "...", self._horizon_url)

        except ImportError:
            log.warning("stellar-sdk not installed — running in simulation mode. "
                        "Install with: pip install stellar-sdk")
            self._public_key = "GSIMULATION000000000000000000000000000000000000000000000"
            self._register_treasury()

    def _register_treasury(self):
        """Register the swarm treasury account."""
        self.accounts["swarm_treasury"] = StellarAgentAccount(
            agent_id="swarm_treasury",
            public_key=self._public_key,
            balance_usdc=self._deposit_amount,
            has_usdc_trustline=True,
        )

    async def _fund_testnet_account(self):
        """Fund account on Stellar testnet via Friendbot."""
        try:
            import aiohttp
            url = f"https://friendbot.stellar.org?addr={self._public_key}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        log.info("Stellar testnet: funded via Friendbot")
                    else:
                        log.warning("Stellar Friendbot failed: %d", resp.status)
        except Exception as e:
            log.warning("Stellar Friendbot error: %s", e)

    async def _on_agent_connected(self, msg: dict):
        """Register a payment account for a newly connected agent."""
        agent_id = msg.get("agent_id", "")
        stellar_address = msg.get("stellar_address", "")
        if agent_id and agent_id not in self.accounts:
            self.accounts[agent_id] = StellarAgentAccount(
                agent_id=agent_id,
                public_key=stellar_address,
            )
            log.info("Stellar: registered account for agent %s", agent_id)

    async def _on_fund_agent(self, msg: dict):
        """Fund an agent's internal balance (for testing/demo)."""
        agent_id = msg.get("agent_id", "")
        amount = msg.get("amount", 10.0)
        if agent_id:
            acct = self.accounts.get(agent_id)
            if not acct:
                acct = StellarAgentAccount(agent_id=agent_id, public_key="")
                self.accounts[agent_id] = acct
            acct.balance_usdc += amount
            log.info("Stellar: funded %s with $%.4f USDC", agent_id, amount)

    async def _on_payment_request(self, msg: dict):
        """Process an x402 payment request on Stellar."""
        from_agent = msg.get("from_agent", "")
        to_agent = msg.get("to_agent", "swarm_treasury")
        service_id = msg.get("service", "")
        amount = msg.get("amount", 0.0)

        if not amount and service_id in self.services:
            amount = self.services[service_id].get("price_usdc", 0.01)

        receipt = await self.pay(from_agent, to_agent, amount, service_id)
        if receipt:
            await self.bus.publish("stellar.payment_confirmed", {
                "receipt": receipt.__dict__,
                "service": service_id,
                "from_agent": from_agent,
            })
        else:
            await self.bus.publish("stellar.payment_rejected", {
                "from_agent": from_agent,
                "reason": "insufficient balance or account not found",
            })

    async def pay(
        self,
        from_agent: str,
        to_agent: str,
        amount: float,
        service: str,
        settle_on_chain: bool = False,
    ) -> StellarPaymentReceipt | None:
        """Execute a payment between two agents.

        By default, payments are tracked in the internal ledger.
        When settle_on_chain=True (or batch threshold reached),
        a real Stellar transaction is submitted.
        """
        sender = self.accounts.get(from_agent)
        receiver = self.accounts.get(to_agent)

        if not sender or not receiver:
            log.warning("Stellar payment failed: unknown agent(s) %s -> %s",
                        from_agent, to_agent)
            return None

        if sender.balance_usdc < amount:
            log.warning("Stellar payment failed: %s has $%.4f, needs $%.4f",
                        from_agent, sender.balance_usdc, amount)
            return None

        # Internal ledger update
        sender.balance_usdc -= amount
        sender.total_spent += amount
        sender.transaction_count += 1

        receiver.balance_usdc += amount
        receiver.total_earned += amount
        receiver.transaction_count += 1

        # Generate tx hash (real or simulated)
        tx_hash = ""
        if settle_on_chain and self._keypair and self._server:
            tx_hash = await self._submit_stellar_payment(
                receiver.public_key, amount
            )
        if not tx_hash:
            tx_hash = hashlib.sha256(
                f"stellar:{from_agent}:{to_agent}:{amount}:{time.time()}".encode()
            ).hexdigest()[:16]

        receipt = StellarPaymentReceipt(
            tx_hash=tx_hash,
            from_agent=from_agent,
            to_agent=to_agent,
            amount_usdc=amount,
            service=service,
            network=self._network,
            stellar_from=sender.public_key,
            stellar_to=receiver.public_key,
        )
        self.receipts.append(receipt)
        if len(self.receipts) > self._max_receipts:
            self.receipts = self.receipts[-self._max_receipts // 2:]

        self._stats["total_payments"] += 1
        self._stats["total_volume_usdc"] += amount

        log.info("Stellar: $%.4f USDC %s → %s for %s [%s]",
                 amount, from_agent, to_agent, service, tx_hash[:12])

        return receipt

    async def _submit_stellar_payment(
        self, destination: str, amount: float
    ) -> str:
        """Submit a real USDC payment on Stellar network."""
        if not self._keypair or not self._server or not destination:
            return ""

        try:
            from stellar_sdk import (
                Asset, Keypair, Network, Server,
                TransactionBuilder,
            )

            usdc = Asset("USDC", self._usdc_issuer)
            source_account = self._server.load_account(self._public_key)

            passphrase = self._net_cfg["passphrase"]

            tx = (
                TransactionBuilder(
                    source_account=source_account,
                    network_passphrase=passphrase,
                    base_fee=100,  # 0.00001 XLM
                )
                .append_payment_op(
                    destination=destination,
                    asset=usdc,
                    amount=f"{amount:.7f}",
                )
                .add_text_memo(f"x402:{amount:.4f}")
                .set_timeout(30)
                .build()
            )

            tx.sign(self._keypair)
            response = self._server.submit_transaction(tx)
            tx_hash = response.get("hash", "")

            self._stats["on_chain_settlements"] += 1
            log.info("Stellar tx submitted: %s (ledger %s)",
                     tx_hash[:16], response.get("ledger"))

            return tx_hash

        except Exception as e:
            self._stats["failed_settlements"] += 1
            log.error("Stellar tx failed: %s", e)
            return ""

    async def submit_xlm_payment(
        self, destination: str, amount_xlm: float, memo: str = ""
    ) -> str:
        """Submit a native XLM payment on Stellar."""
        if not self._keypair or not self._server or not destination:
            return ""

        try:
            from stellar_sdk import (
                Asset, Keypair, Network, Server,
                TransactionBuilder,
            )

            source_account = self._server.load_account(self._public_key)
            passphrase = self._net_cfg["passphrase"]

            builder = TransactionBuilder(
                source_account=source_account,
                network_passphrase=passphrase,
                base_fee=100,
            )
            builder.append_payment_op(
                destination=destination,
                asset=Asset.native(),
                amount=f"{amount_xlm:.7f}",
            )
            if memo:
                builder.add_text_memo(memo[:28])
            builder.set_timeout(30)

            tx = builder.build()
            tx.sign(self._keypair)
            response = self._server.submit_transaction(tx)
            tx_hash = response.get("hash", "")

            log.info("Stellar XLM tx: %s → %s %.4f XLM",
                     tx_hash[:16], destination[:12], amount_xlm)
            self._stats["total_volume_xlm"] += amount_xlm
            return tx_hash

        except Exception as e:
            log.error("Stellar XLM tx failed: %s", e)
            return ""

    async def setup_usdc_trustline(self) -> bool:
        """Establish USDC trustline on Stellar account."""
        if not self._keypair or not self._server:
            return False

        try:
            from stellar_sdk import (
                Asset, TransactionBuilder,
            )

            usdc = Asset("USDC", self._usdc_issuer)
            source_account = self._server.load_account(self._public_key)
            passphrase = self._net_cfg["passphrase"]

            tx = (
                TransactionBuilder(
                    source_account=source_account,
                    network_passphrase=passphrase,
                    base_fee=100,
                )
                .append_change_trust_op(asset=usdc)
                .set_timeout(30)
                .build()
            )

            tx.sign(self._keypair)
            response = self._server.submit_transaction(tx)
            log.info("Stellar USDC trustline established: %s", response.get("hash", "")[:16])
            return True

        except Exception as e:
            log.error("Stellar trustline failed: %s", e)
            return False

    async def query_sdex_orderbook(
        self, selling_asset_code: str = "XLM", buying_asset_code: str = "USDC"
    ) -> dict:
        """Query the Stellar DEX (SDEX) orderbook for a trading pair."""
        if not self._server:
            return {}

        try:
            from stellar_sdk import Asset

            if selling_asset_code == "XLM":
                selling = Asset.native()
            else:
                selling = Asset(selling_asset_code, self._usdc_issuer)

            if buying_asset_code == "USDC":
                buying = Asset("USDC", self._usdc_issuer)
            elif buying_asset_code == "XLM":
                buying = Asset.native()
            else:
                buying = Asset(buying_asset_code, self._usdc_issuer)

            orderbook = self._server.orderbook(selling, buying).limit(20).call()

            bids = [{"price": float(b["price"]), "amount": float(b["amount"])}
                    for b in orderbook.get("bids", [])[:10]]
            asks = [{"price": float(a["price"]), "amount": float(a["amount"])}
                    for a in orderbook.get("asks", [])[:10]]

            return {
                "pair": f"{selling_asset_code}/{buying_asset_code}",
                "bids": bids,
                "asks": asks,
                "spread": (asks[0]["price"] - bids[0]["price"]) if bids and asks else 0,
                "mid_price": (asks[0]["price"] + bids[0]["price"]) / 2 if bids and asks else 0,
            }

        except Exception as e:
            log.warning("SDEX orderbook query failed: %s", e)
            return {}

    async def place_sdex_offer(
        self,
        selling_code: str,
        buying_code: str,
        amount: float,
        price: float,
    ) -> str:
        """Place a limit order on the Stellar DEX (SDEX)."""
        if not self._keypair or not self._server:
            return ""

        try:
            from stellar_sdk import Asset, TransactionBuilder

            if selling_code == "XLM":
                selling = Asset.native()
            else:
                selling = Asset(selling_code, self._usdc_issuer)

            if buying_code == "XLM":
                buying = Asset.native()
            else:
                buying = Asset(buying_code, self._usdc_issuer)

            source_account = self._server.load_account(self._public_key)
            passphrase = self._net_cfg["passphrase"]

            tx = (
                TransactionBuilder(
                    source_account=source_account,
                    network_passphrase=passphrase,
                    base_fee=100,
                )
                .append_manage_sell_offer_op(
                    selling=selling,
                    buying=buying,
                    amount=f"{amount:.7f}",
                    price=f"{price:.7f}",
                )
                .add_text_memo("swarm:sdex")
                .set_timeout(30)
                .build()
            )

            tx.sign(self._keypair)
            response = self._server.submit_transaction(tx)
            tx_hash = response.get("hash", "")
            log.info("SDEX offer placed: sell %.4f %s @ %.4f %s [%s]",
                     amount, selling_code, price, buying_code, tx_hash[:16])
            return tx_hash

        except Exception as e:
            log.error("SDEX offer failed: %s", e)
            return ""

    async def invoke_soroban_contract(
        self, contract_id: str, function_name: str, args: list | None = None
    ) -> dict:
        """Invoke a Soroban smart contract on Stellar.

        Used for programmable spending policies, multi-sig agent wallets,
        and on-chain agent reputation.
        """
        if not self._keypair or not self._server:
            return {"error": "not initialized"}

        try:
            from stellar_sdk import SorobanServer, TransactionBuilder
            from stellar_sdk import scval

            soroban_url = self._horizon_url.replace("horizon", "soroban-rpc")
            if "testnet" in self._horizon_url:
                soroban_url = "https://soroban-testnet.stellar.org"
            else:
                soroban_url = "https://soroban-rpc.mainnet.stellar.gateway.fm"

            soroban = SorobanServer(soroban_url)
            source_account = soroban.load_account(self._public_key)
            passphrase = self._net_cfg["passphrase"]

            # Build the contract invocation
            builder = TransactionBuilder(
                source_account=source_account,
                network_passphrase=passphrase,
                base_fee=100,
            )
            builder.set_timeout(30)
            builder.append_invoke_contract_function_op(
                contract_id=contract_id,
                function_name=function_name,
                parameters=args or [],
            )

            tx = builder.build()

            # Simulate first
            sim_response = soroban.simulate_transaction(tx)
            if sim_response.error:
                return {"error": str(sim_response.error)}

            # Prepare and submit
            tx = soroban.prepare_transaction(tx, sim_response)
            tx.sign(self._keypair)
            send_response = soroban.send_transaction(tx)

            return {
                "status": send_response.status,
                "hash": send_response.hash,
                "contract": contract_id,
                "function": function_name,
            }

        except ImportError:
            log.warning("Soroban support requires stellar-sdk >= 9.0")
            return {"error": "stellar-sdk version too old for Soroban"}
        except Exception as e:
            log.error("Soroban invocation failed: %s", e)
            return {"error": str(e)}

    def register_service(self, service_id: str, description: str,
                         price_usdc: float, rate_limit: int = 100):
        """Register a paid service offered by the swarm."""
        self.services[service_id] = {
            "service_id": service_id,
            "description": description,
            "price_usdc": price_usdc,
            "rate_limit": rate_limit,
        }

    # ── x402 HTTP 402 Protocol (challenge/response) ──────────────

    def create_402_challenge(self, service_id: str) -> dict:
        """Create an HTTP 402 Payment Required challenge for a service.

        Returns the challenge payload that goes in the X-Payment-Required
        header when a client requests a paid endpoint without payment.

        This follows the x402 spec: the client must respond with a signed
        Soroban SAC transfer authorization entry.
        """
        svc = self.services.get(service_id)
        if not svc:
            return {}

        # Amount in base units (USDC has 7 decimal places on Stellar)
        amount_stroops = int(svc["price_usdc"] * 10_000_000)

        return {
            "x402Version": 2,
            "scheme": "exact-v2",
            "network": f"stellar:{self._network}",
            "amount": str(amount_stroops),
            "asset": self._soroban_usdc,
            "recipient": self._public_key,
            "service": service_id,
            "description": svc["description"],
            "price_human": f"${svc['price_usdc']:.4f} USDC",
            "facilitator": os.getenv(
                "STELLAR_FACILITATOR_URL",
                "https://channels.openzeppelin.com/x402/testnet",
            ),
        }

    async def verify_stellar_payment(self, tx_hash: str, expected_amount: float,
                                     expected_recipient: str = "") -> dict:
        """Verify a Stellar payment on-chain via Horizon API.

        Checks that the transaction:
        1. Exists and was successful
        2. Contains a payment operation
        3. Pays the correct amount to the correct recipient
        4. Uses USDC (or native XLM)
        """
        if not self._server or not tx_hash:
            return {"verified": False, "error": "no server or tx_hash"}

        recipient = expected_recipient or self._public_key

        try:
            # Fetch transaction from Horizon
            tx = self._server.transactions().transaction(tx_hash).call()

            if not tx.get("successful", False):
                return {"verified": False, "error": "transaction failed on-chain"}

            # Fetch operations for this transaction
            ops = self._server.operations().for_transaction(tx_hash).call()
            records = ops.get("_embedded", {}).get("records", [])

            for op in records:
                if op.get("type") != "payment":
                    continue

                op_to = op.get("to", "")
                op_amount = float(op.get("amount", "0"))
                op_asset = op.get("asset_code", "XLM")

                if (op_to == recipient and
                    op_amount >= expected_amount * 0.99 and  # 1% tolerance
                    op_asset in ("USDC", "XLM")):
                    return {
                        "verified": True,
                        "tx_hash": tx_hash,
                        "from": op.get("from", ""),
                        "to": op_to,
                        "amount": op_amount,
                        "asset": op_asset,
                        "ledger": tx.get("ledger"),
                        "created_at": tx.get("created_at"),
                    }

            return {"verified": False, "error": "no matching payment operation found"}

        except Exception as e:
            return {"verified": False, "error": str(e)}

    async def handle_x402_request(
        self, service_id: str, authorization: str = "", tx_hash: str = "",
    ) -> tuple[int, dict]:
        """Handle an incoming HTTP request that may require x402 payment.

        Returns (status_code, response_body):
        - (402, challenge) if no payment provided
        - (200, {"paid": True, ...}) if payment verified
        - (403, {"error": ...}) if payment invalid
        """
        svc = self.services.get(service_id)
        if not svc:
            return 404, {"error": f"unknown service: {service_id}"}

        # No payment provided → return 402 challenge
        if not authorization and not tx_hash:
            challenge = self.create_402_challenge(service_id)
            return 402, {
                "type": "https://paymentauth.org/problems/payment-required",
                "title": "Payment Required",
                "status": 402,
                "detail": f"This endpoint requires ${svc['price_usdc']:.4f} USDC per request",
                "challenge": challenge,
            }

        # Payment provided → verify on-chain
        payment_hash = tx_hash or authorization
        result = await self.verify_stellar_payment(
            payment_hash, svc["price_usdc"]
        )

        if result.get("verified"):
            self._stats["total_payments"] += 1
            self._stats["total_volume_usdc"] += svc["price_usdc"]
            self._stats["on_chain_settlements"] += 1
            return 200, {
                "paid": True,
                "receipt": result,
                "service": service_id,
            }
        else:
            return 403, {
                "error": "payment verification failed",
                "detail": result.get("error", "unknown"),
            }

    # ── MPP (Machine Payments Protocol) ────────────────────────

    def create_mpp_charge(self, service_id: str) -> dict:
        """Create an MPP charge intent for a service.

        MPP charge intents use Soroban SAC transfer for one-time payments.
        The client signs auth entries and the server broadcasts the tx.
        """
        svc = self.services.get(service_id)
        if not svc:
            return {}

        amount_stroops = int(svc["price_usdc"] * 10_000_000)

        return {
            "protocol": "mpp",
            "version": "1.0",
            "intent": "charge",
            "mode": "pull",  # client signs, server broadcasts
            "network": f"stellar:{self._network}",
            "payment": {
                "asset": self._soroban_usdc,
                "amount": str(amount_stroops),
                "recipient": self._public_key,
            },
            "service": {
                "id": service_id,
                "description": svc["description"],
                "price_human": f"${svc['price_usdc']:.4f} USDC",
            },
        }

    def get_service_catalog(self) -> list[dict]:
        """Return the service catalog for external agents.

        Each service includes both x402 and MPP pricing info so
        clients can choose their preferred payment protocol.
        """
        catalog = []
        for svc in self.services.values():
            amount_stroops = int(svc["price_usdc"] * 10_000_000)
            catalog.append({
                **svc,
                "x402": {
                    "scheme": "exact-v2",
                    "asset": self._soroban_usdc,
                    "amount": str(amount_stroops),
                    "recipient": self._public_key,
                    "network": f"stellar:{self._network}",
                },
                "mpp": {
                    "intent": "charge",
                    "asset": self._soroban_usdc,
                    "amount": str(amount_stroops),
                },
            })
        return catalog

    def status(self) -> dict:
        return {
            "network": self._network,
            "horizon": self._horizon_url,
            "public_key": self._public_key[:16] + "..." if self._public_key else "",
            "usdc_issuer": self._usdc_issuer[:16] + "...",
            "soroban_usdc": self._soroban_usdc[:16] + "..." if self._soroban_usdc else "",
            "accounts": len(self.accounts),
            "services": len(self.services),
            "stats": self._stats,
            "accounts_detail": {
                aid: {
                    "balance_usdc": round(a.balance_usdc, 6),
                    "earned": round(a.total_earned, 6),
                    "spent": round(a.total_spent, 6),
                    "txns": a.transaction_count,
                }
                for aid, a in self.accounts.items()
            },
        }


# ── Default services the swarm offers on Stellar ─────────────────

DEFAULT_STELLAR_SERVICES = [
    ("signal.realtime", "Real-time trading signals", 0.01, 60),
    ("signal.smart_money", "Smart money wallet tracking", 0.02, 30),
    ("signal.confluence", "Multi-indicator confluence signals", 0.015, 30),
    ("data.market_snapshot", "Current market snapshot", 0.001, 300),
    ("data.sdex_orderbook", "Stellar DEX orderbook data", 0.002, 120),
    ("data.portfolio", "Portfolio state snapshot", 0.005, 60),
    ("analysis.var", "Value-at-Risk computation", 0.05, 10),
    ("analysis.stress_test", "Stress test scenario", 0.10, 5),
    ("execution.sdex_quote", "SDEX order routing quote", 0.003, 60),
    ("intelligence.alpha", "Alpha signal package", 0.10, 10),
    ("stellar.pathfinding", "Stellar path payment optimization", 0.005, 30),
]


def create_stellar_gateway(bus: Bus, **kwargs) -> StellarPaymentGateway:
    """Factory function to create and configure a Stellar payment gateway."""
    gw = StellarPaymentGateway(bus, **kwargs)
    for sid, desc, price, rate in DEFAULT_STELLAR_SERVICES:
        gw.register_service(sid, desc, price, rate)
    return gw
