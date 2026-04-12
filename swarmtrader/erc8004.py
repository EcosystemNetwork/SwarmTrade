"""
ERC-8004 Trustless Agent Integration.

Provides on-chain agent identity, reputation tracking, and trade validation
via the ERC-8004 registries on Ethereum (Sepolia testnet or mainnet).

Architecture:
  - AgentIdentity: Registers the trading agent as an ERC-721 NFT
  - ReputationRecorder: Posts trade outcomes to the Reputation Registry
  - ValidationRecorder: Records risk check artifacts to the Validation Registry
  - TradeIntentSigner: Creates EIP-712 signed trade intents for trustless execution
"""
from __future__ import annotations
import asyncio, hashlib, json, logging, os, time
from dataclasses import asdict
from typing import Any

from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3

from .core import Bus, TradeIntent, ExecutionReport, RiskVerdict, Signal

log = logging.getLogger("swarm.erc8004")

# ---------------------------------------------------------------------------
# Contract addresses (Sepolia testnet)
# ---------------------------------------------------------------------------
NETWORKS = {
    "sepolia": {
        "rpc": "https://rpc.sepolia.org",
        "chain_id": 11155111,
        "identity_registry": "0x8004A818BFB912233c491871b3d84c89A494BD9e",
        "reputation_registry": "0x8004B663056A597Dffe9eCcC1965A193B7388713",
        # Nuwa reference validation registry
        "validation_registry": "0x662b40A526cb4017d947e71eAF6753BF3eeE66d8",
    },
    "mainnet": {
        "rpc": "https://eth.llamarpc.com",
        "chain_id": 1,
        "identity_registry": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
        "reputation_registry": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
        "validation_registry": None,
    },
    "base": {
        "rpc": "https://mainnet.base.org",
        "chain_id": 8453,
        "identity_registry": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432",
        "reputation_registry": "0x8004BAa17C55a88189AE136b182e5fdA19dE9b63",
        "validation_registry": None,
    },
}

# ---------------------------------------------------------------------------
# ABI fragments (minimal — just the functions we need)
# ---------------------------------------------------------------------------
IDENTITY_ABI = json.loads("""[
    {
        "inputs": [{"internalType": "string", "name": "agentURI", "type": "string"}],
        "name": "register",
        "outputs": [{"internalType": "uint256", "name": "agentId", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "agentId", "type": "uint256"},
            {"internalType": "string", "name": "key", "type": "string"},
            {"internalType": "bytes", "name": "value", "type": "bytes"}
        ],
        "name": "setMetadata",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "agentId", "type": "uint256"},
            {"internalType": "string", "name": "key", "type": "string"}
        ],
        "name": "getMetadata",
        "outputs": [{"internalType": "bytes", "name": "value", "type": "bytes"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "ownerOf",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "tokenId", "type": "uint256"}],
        "name": "tokenURI",
        "outputs": [{"internalType": "string", "name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "anonymous": false,
        "inputs": [
            {"indexed": true, "internalType": "address", "name": "from", "type": "address"},
            {"indexed": true, "internalType": "address", "name": "to", "type": "address"},
            {"indexed": true, "internalType": "uint256", "name": "tokenId", "type": "uint256"}
        ],
        "name": "Transfer",
        "type": "event"
    }
]""")

REPUTATION_ABI = json.loads("""[
    {
        "inputs": [
            {"internalType": "uint256", "name": "agentId", "type": "uint256"},
            {"internalType": "uint8", "name": "score", "type": "uint8"},
            {"internalType": "bytes32", "name": "tag1", "type": "bytes32"},
            {"internalType": "bytes32", "name": "tag2", "type": "bytes32"},
            {"internalType": "string", "name": "fileuri", "type": "string"},
            {"internalType": "bytes32", "name": "filehash", "type": "bytes32"},
            {"internalType": "bytes", "name": "feedbackAuth", "type": "bytes"}
        ],
        "name": "giveFeedback",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "agentId", "type": "uint256"},
            {"internalType": "address", "name": "clientAddress", "type": "address"},
            {"internalType": "uint64", "name": "index", "type": "uint64"}
        ],
        "name": "readFeedback",
        "outputs": [
            {"internalType": "uint8", "name": "score", "type": "uint8"},
            {"internalType": "bytes32", "name": "tag1", "type": "bytes32"},
            {"internalType": "bytes32", "name": "tag2", "type": "bytes32"},
            {"internalType": "bool", "name": "isRevoked", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]""")

VALIDATION_ABI = json.loads("""[
    {
        "inputs": [
            {"internalType": "address", "name": "validatorAddress", "type": "address"},
            {"internalType": "uint256", "name": "agentId", "type": "uint256"},
            {"internalType": "string", "name": "requestUri", "type": "string"},
            {"internalType": "bytes32", "name": "requestHash", "type": "bytes32"}
        ],
        "name": "validationRequest",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "bytes32", "name": "requestHash", "type": "bytes32"},
            {"internalType": "uint8", "name": "response", "type": "uint8"},
            {"internalType": "string", "name": "responseUri", "type": "string"},
            {"internalType": "bytes32", "name": "responseHash", "type": "bytes32"},
            {"internalType": "bytes32", "name": "tag", "type": "bytes32"}
        ],
        "name": "validationResponse",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]""")


def _bytes32(s: str) -> bytes:
    """Encode a string as bytes32 (right-padded)."""
    b = s.encode("utf-8")[:32]
    return b + b"\x00" * (32 - len(b))


def _hash(data: str) -> bytes:
    """SHA-256 hash as bytes32."""
    return hashlib.sha256(data.encode()).digest()


# ---------------------------------------------------------------------------
# ERC-8004 Agent Manager
# ---------------------------------------------------------------------------
class ERC8004Agent:
    """Manages on-chain agent identity, reputation, and validation via ERC-8004."""

    def __init__(
        self,
        bus: Bus,
        private_key: str,
        network: str = "sepolia",
        agent_uri: str = "",
        auto_register: bool = True,
    ):
        self.bus = bus
        self.network = network
        self.config = NETWORKS[network]

        # Web3 setup
        self.w3 = Web3(Web3.HTTPProvider(self.config["rpc"]))
        self.account = Account.from_key(private_key)
        self.address = self.account.address

        # Contracts
        self.identity = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.config["identity_registry"]),
            abi=IDENTITY_ABI,
        )
        self.reputation = self.w3.eth.contract(
            address=Web3.to_checksum_address(self.config["reputation_registry"]),
            abi=REPUTATION_ABI,
        )
        if self.config.get("validation_registry"):
            self.validation = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.config["validation_registry"]),
                abi=VALIDATION_ABI,
            )
        else:
            self.validation = None

        self.agent_id: int | None = None
        self.agent_uri = agent_uri or self._default_agent_uri()
        self.auto_register = auto_register
        self._feedback_count = 0

        # Subscribe to bus events
        bus.subscribe("exec.report", self._on_report)
        bus.subscribe("risk.verdict", self._on_verdict)

    def _default_agent_uri(self) -> str:
        """Generate a data URI for the agent registration JSON."""
        reg = {
            "type": "https://eips.ethereum.org/EIPS/eip-8004#registration-v1",
            "name": "SwarmTrader",
            "description": "Multi-agent autonomous trading platform with regime-aware strategy, "
                           "Kelly criterion sizing, and multi-signal fusion.",
            "endpoints": [
                {"name": "agentWallet", "endpoint": f"eip155:{self.config['chain_id']}:{self.address}"},
            ],
            "supportedTrust": ["reputation", "validation"],
            "capabilities": [
                "spot-trading", "risk-management", "multi-agent-consensus",
                "regime-detection", "backtesting",
            ],
        }
        # Use data URI for simplicity (IPFS would be better for production)
        return "data:application/json;base64," + __import__("base64").b64encode(
            json.dumps(reg).encode()
        ).decode()

    async def register(self) -> int:
        """Register agent on the ERC-8004 Identity Registry. Returns agent ID."""
        log.info("Registering agent on ERC-8004 (%s)...", self.network)
        log.info("  Address: %s", self.address)

        try:
            nonce = self.w3.eth.get_transaction_count(self.address)
            tx = self.identity.functions.register(self.agent_uri).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "gas": 200_000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": self.config["chain_id"],
            })
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            log.info("  Registration tx: %s", tx_hash.hex())

            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            if receipt["status"] != 1:
                raise RuntimeError(f"Registration failed: tx {tx_hash.hex()}")

            # Extract agent ID from Transfer event
            transfer_logs = self.identity.events.Transfer().process_receipt(receipt)
            if transfer_logs:
                self.agent_id = transfer_logs[0]["args"]["tokenId"]
            else:
                # Fallback: parse from logs
                self.agent_id = int(receipt["logs"][0]["topics"][3].hex(), 16)

            log.info("  Agent registered! ID: %d", self.agent_id)

            # Set metadata
            await self._set_metadata("version", "0.1.0")
            await self._set_metadata("type", "trading-agent")
            await self._set_metadata("strategy", "multi-agent-swarm")

            return self.agent_id

        except Exception as e:
            log.error("Registration failed: %s", e)
            raise

    async def _set_metadata(self, key: str, value: str):
        """Set on-chain metadata for the agent."""
        if self.agent_id is None:
            return
        try:
            nonce = self.w3.eth.get_transaction_count(self.address)
            tx = self.identity.functions.setMetadata(
                self.agent_id, key, value.encode("utf-8")
            ).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "gas": 100_000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": self.config["chain_id"],
            })
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            log.debug("  Metadata set: %s=%s", key, value)
        except Exception as e:
            log.warning("  Failed to set metadata %s: %s", key, e)

    async def _on_report(self, rep: ExecutionReport):
        """Post trade outcome to Reputation Registry."""
        if self.agent_id is None or rep.status != "filled":
            return

        pnl = rep.pnl_estimate or 0.0
        # Score: 0-100, centered at 50 (break-even), scaled by PnL magnitude
        score = max(0, min(100, int(50 + pnl * 100)))

        tag1 = _bytes32("trade-execution")
        tag2 = _bytes32("profitable" if pnl > 0 else "loss")

        # Feedback details
        feedback_data = json.dumps({
            "intent_id": rep.intent_id,
            "status": rep.status,
            "fill_price": rep.fill_price,
            "slippage": rep.realized_slippage,
            "pnl": pnl,
            "timestamp": time.time(),
        })
        feedback_hash = _hash(feedback_data)

        try:
            # Sign the feedback
            msg_hash = Web3.solidity_keccak(
                ["uint256", "uint8", "bytes32", "bytes32"],
                [self.agent_id, score, tag1, tag2],
            )
            sig = self.account.signHash(msg_hash)
            feedback_auth = sig.signature

            nonce = self.w3.eth.get_transaction_count(self.address)
            tx = self.reputation.functions.giveFeedback(
                self.agent_id, score, tag1, tag2,
                "",  # No external URI for now
                feedback_hash,
                feedback_auth,
            ).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "gas": 300_000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": self.config["chain_id"],
            })
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            log.info("REPUTATION posted: score=%d pnl=%+.4f tx=%s",
                     score, pnl, tx_hash.hex()[:16])
            self._feedback_count += 1

        except Exception as e:
            log.debug("Reputation post failed (non-critical): %s", e)

    async def _on_verdict(self, verdict: RiskVerdict):
        """Record risk check validation artifacts on-chain."""
        if self.agent_id is None or self.validation is None:
            return

        # Only record rejections (more interesting for trust model)
        if verdict.approve:
            return

        try:
            request_data = json.dumps({
                "intent_id": verdict.intent_id,
                "agent": verdict.agent_id,
                "approved": verdict.approve,
                "reason": verdict.reason,
                "timestamp": time.time(),
            })
            request_hash = _hash(request_data)

            nonce = self.w3.eth.get_transaction_count(self.address)
            tx = self.validation.functions.validationRequest(
                self.address,  # self-validation
                self.agent_id,
                "",  # No external URI
                request_hash,
            ).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "gas": 350_000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": self.config["chain_id"],
            })
            signed = self.account.sign_transaction(tx)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)

            # Post the validation response (risk check result)
            self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            response_score = 0 if not verdict.approve else 100
            nonce = self.w3.eth.get_transaction_count(self.address)
            tx2 = self.validation.functions.validationResponse(
                request_hash,
                response_score,
                "",
                _hash(request_data + f"_response_{response_score}"),
                _bytes32(f"risk-{verdict.agent_id}"),
            ).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "gas": 450_000,
                "gasPrice": self.w3.eth.gas_price,
                "chainId": self.config["chain_id"],
            })
            signed2 = self.account.sign_transaction(tx2)
            tx_hash2 = self.w3.eth.send_raw_transaction(signed2.raw_transaction)
            log.info("VALIDATION recorded: %s rejected by %s tx=%s",
                     verdict.intent_id, verdict.agent_id, tx_hash2.hex()[:16])

        except Exception as e:
            log.debug("Validation recording failed (non-critical): %s", e)

    def status(self) -> dict:
        """Return current ERC-8004 status."""
        return {
            "network": self.network,
            "address": self.address,
            "agent_id": self.agent_id,
            "feedback_count": self._feedback_count,
            "identity_registry": self.config["identity_registry"],
            "reputation_registry": self.config["reputation_registry"],
        }


# ---------------------------------------------------------------------------
# EIP-712 Trade Intent Signer
# ---------------------------------------------------------------------------
class TradeIntentSigner:
    """Signs trade intents using EIP-712 typed data for trustless verification."""

    DOMAIN_NAME = "SwarmTrader"
    DOMAIN_VERSION = "1"

    def __init__(self, private_key: str, chain_id: int = 11155111,
                 verifying_contract: str = "0x0000000000000000000000000000000000000000"):
        self.account = Account.from_key(private_key)
        self.chain_id = chain_id
        self.verifying_contract = verifying_contract

    def sign_intent(self, intent: TradeIntent) -> dict:
        """Create an EIP-712 signed trade intent."""
        domain_data = {
            "name": self.DOMAIN_NAME,
            "version": self.DOMAIN_VERSION,
            "chainId": self.chain_id,
            "verifyingContract": self.verifying_contract,
        }

        message_types = {
            "TradeIntent": [
                {"name": "id", "type": "string"},
                {"name": "assetIn", "type": "string"},
                {"name": "assetOut", "type": "string"},
                {"name": "amountIn", "type": "uint256"},
                {"name": "minOut", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        }

        message_data = {
            "id": intent.id,
            "assetIn": intent.asset_in,
            "assetOut": intent.asset_out,
            "amountIn": int(intent.amount_in * 1e6),  # 6 decimals (USDC-style)
            "minOut": int(intent.min_out * 1e6),
            "deadline": int(intent.ttl),
        }

        # Sign using EIP-712
        signable = encode_typed_data(
            domain_data, message_types, message_data,
        )
        signed = self.account.sign_message(signable)

        return {
            "intent": message_data,
            "domain": domain_data,
            "signature": signed.signature.hex(),
            "signer": self.account.address,
            "v": signed.v,
            "r": hex(signed.r),
            "s": hex(signed.s),
        }


# ---------------------------------------------------------------------------
# Bus integration: auto-sign intents and publish signed versions
# ---------------------------------------------------------------------------
class ERC8004Pipeline:
    """Wires ERC-8004 into the SwarmTrader bus:
    1. On startup: register agent identity
    2. On trade: post reputation feedback
    3. On risk rejection: record validation artifact
    4. On intent: sign with EIP-712
    """

    def __init__(self, bus: Bus, private_key: str, network: str = "sepolia",
                 agent_uri: str = ""):
        self.bus = bus
        self.agent = ERC8004Agent(bus, private_key, network, agent_uri)
        self.signer = TradeIntentSigner(
            private_key,
            chain_id=NETWORKS[network]["chain_id"],
        )

        # Sign intents before they go to risk
        bus.subscribe("intent.new", self._on_intent)

    async def start(self):
        """Register agent and start ERC-8004 pipeline."""
        if self.agent.auto_register:
            try:
                await self.agent.register()
            except Exception as e:
                log.warning("ERC-8004 registration failed (continuing without): %s", e)

    async def _on_intent(self, intent: TradeIntent):
        """Sign each trade intent with EIP-712."""
        try:
            signed = self.signer.sign_intent(intent)
            log.debug("Intent %s signed: %s", intent.id, signed["signature"][:20])
            await self.bus.publish("intent.signed", {
                "intent_id": intent.id,
                "signed_data": signed,
            })
        except Exception as e:
            log.debug("Intent signing failed: %s", e)

    def status(self) -> dict:
        return self.agent.status()
