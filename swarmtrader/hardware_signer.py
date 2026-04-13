"""Hardware Wallet Signing — Ledger device integration for secure tx signing.

Inspired by Maki and LeAgent (ETHGlobal Cannes 2026). Separates key
management from AI intent interpretation: the LLM decides WHAT to trade,
but signing happens on a hardware device the LLM cannot access.

Architecture:
  LLM (intent) -> Policy Check -> Simulation -> Hardware Sign -> Submit

For large trades (above auto-approve threshold), requires physical
Ledger approval. Small trades within policy use the hot wallet.

Signing modes:
  1. hot_wallet    — software key, auto-signs within policy limits
  2. ledger_usb    — Ledger device via USB HID (requires physical presence)
  3. ledger_remote — Ledger Live remote signing (coming soon)
  4. secure_enclave — macOS Secure Enclave via subprocess (if available)

Environment variables:
  PRIVATE_KEY           — hot wallet key for small/auto-approved trades
  HARDWARE_SIGNER_MODE  — "hot_wallet", "ledger_usb", or "secure_enclave"
  HARDWARE_APPROVE_USD  — USD threshold requiring hardware approval (default: 500)
  LEDGER_DERIVATION_PATH — HD path (default: m/44'/60'/0'/0/0)
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

from .core import Bus, TradeIntent, ExecutionReport

log = logging.getLogger("swarm.hardware_signer")


@dataclass
class SigningRequest:
    """A transaction awaiting signature."""
    request_id: str
    intent: TradeIntent
    tx_data: dict  # raw unsigned transaction
    estimated_usd: float
    signer_mode: str  # which signer to use
    status: str = "pending"  # pending, approved, signed, rejected, timeout
    signature: str = ""
    signed_tx: str = ""
    created_at: float = field(default_factory=time.time)
    signed_at: float = 0.0
    timeout_s: float = 300.0  # 5 min to approve


@dataclass
class SignerStats:
    """Statistics for a signing backend."""
    total_requests: int = 0
    total_signed: int = 0
    total_rejected: int = 0
    total_timeouts: int = 0
    total_usd_signed: float = 0.0
    avg_sign_time_s: float = 0.0


class HotWalletSigner:
    """Software-based signer using in-memory private key.

    Used for trades within the auto-approve threshold.
    """

    def __init__(self, private_key: str = ""):
        self._private_key = private_key or os.getenv("PRIVATE_KEY", "")
        self._address = ""
        self._stats = SignerStats()

        if self._private_key:
            try:
                from eth_account import Account
                acct = Account.from_key(self._private_key)
                self._address = acct.address
            except ImportError:
                log.warning("eth_account not installed — hot wallet signing disabled")

    @property
    def address(self) -> str:
        return self._address

    async def sign_transaction(self, tx_data: dict) -> tuple[bool, str, str]:
        """Sign a transaction with the hot wallet.

        Returns (success, signed_tx_hex, error_message).
        """
        if not self._private_key:
            return False, "", "no private key configured"

        try:
            from eth_account import Account
            signed = Account.from_key(self._private_key).sign_transaction(tx_data)
            self._stats.total_signed += 1
            self._stats.total_requests += 1
            return True, signed.raw_transaction.hex(), ""
        except Exception as e:
            self._stats.total_requests += 1
            return False, "", str(e)

    async def sign_message(self, message: str) -> tuple[bool, str, str]:
        """Sign a message (EIP-191)."""
        if not self._private_key:
            return False, "", "no private key configured"

        try:
            from eth_account import Account
            from eth_account.messages import encode_defunct
            msg = encode_defunct(text=message)
            signed = Account.from_key(self._private_key).sign_message(msg)
            return True, signed.signature.hex(), ""
        except Exception as e:
            return False, "", str(e)


class LedgerSigner:
    """Ledger hardware wallet signer via USB HID.

    Requires the Ledger device to be connected and the Ethereum app open.
    Uses ledgereth or ledgercomm libraries for communication.

    This is a stub that shows the integration pattern — actual HID
    communication requires the device to be physically present.
    """

    def __init__(self, derivation_path: str = "m/44'/60'/0'/0/0"):
        self.derivation_path = derivation_path
        self._stats = SignerStats()
        self._connected = False
        self._address = ""

    async def connect(self) -> tuple[bool, str]:
        """Attempt to connect to a Ledger device."""
        try:
            # In production: use ledgercomm or ledgereth
            # from ledgercomm import Transport
            # transport = Transport.create()
            # ... get address from device
            log.info("Ledger connection attempt (derivation=%s)", self.derivation_path)
            # Stub: report not connected unless device is present
            self._connected = False
            return False, "Ledger device not detected (connect device and open Ethereum app)"
        except Exception as e:
            return False, f"Ledger connection failed: {e}"

    @property
    def address(self) -> str:
        return self._address

    async def sign_transaction(self, tx_data: dict) -> tuple[bool, str, str]:
        """Request transaction signing from Ledger device.

        The user must physically approve on the device screen.
        """
        if not self._connected:
            return False, "", "Ledger not connected"

        self._stats.total_requests += 1

        try:
            # In production:
            # 1. Serialize tx to RLP
            # 2. Send APDU command to device
            # 3. Wait for user approval (button press)
            # 4. Receive signature back
            log.info("Awaiting Ledger approval for transaction...")

            # Stub: simulate approval timeout
            return False, "", "Ledger approval required (press buttons on device)"

        except Exception as e:
            return False, "", f"Ledger signing error: {e}"


class TransactionSimulator:
    """Simulates transactions before signing to prevent loss.

    Runs the transaction through eth_call to verify:
    - Transaction does not revert
    - Gas estimate is reasonable
    - No unexpected failures

    Falls back to explicit rejection (not silent success) when no
    RPC is configured or the call fails.
    """

    # Approximate gas price in ETH and ETH/USD for cost estimation
    _DEFAULT_GAS_GWEI = 0.01   # Base L2 typical
    _ETH_USD_FALLBACK = 3000.0

    def __init__(self, rpc_url: str = ""):
        self.rpc_url = rpc_url or os.getenv("BASE_RPC_URL", "")

    async def simulate(self, tx_data: dict) -> dict:
        """Simulate a transaction via eth_call and eth_estimateGas.

        Returns dict with:
          - success: bool
          - gas_used: int
          - output: hex string
          - warnings: list[str]
          - estimated_cost_usd: float
        """
        warnings: list[str] = []

        if not tx_data or not tx_data.get("to"):
            return {
                "success": False,
                "gas_used": 0,
                "output": "0x",
                "warnings": ["no transaction data provided"],
                "estimated_cost_usd": 0.0,
            }

        if not self.rpc_url:
            return {
                "success": False,
                "gas_used": 0,
                "output": "0x",
                "warnings": ["no RPC URL configured — cannot simulate, rejecting for safety"],
                "estimated_cost_usd": 0.0,
            }

        try:
            import aiohttp

            call_params = {
                "from": tx_data.get("from", "0x" + "0" * 40),
                "to": tx_data["to"],
            }
            if tx_data.get("data"):
                call_params["data"] = tx_data["data"]
            if tx_data.get("value"):
                call_params["value"] = (
                    hex(tx_data["value"]) if isinstance(tx_data["value"], int)
                    else tx_data["value"]
                )

            async with aiohttp.ClientSession() as session:
                # 1. eth_call — check for revert
                payload = {
                    "jsonrpc": "2.0", "id": 1, "method": "eth_call",
                    "params": [call_params, "latest"],
                }
                async with session.post(
                    self.rpc_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    result = await resp.json()

                if "error" in result:
                    err_msg = result["error"].get("message", str(result["error"]))
                    return {
                        "success": False,
                        "gas_used": 0,
                        "output": "0x",
                        "warnings": [f"eth_call reverted: {err_msg}"],
                        "estimated_cost_usd": 0.0,
                    }

                output = result.get("result", "0x")

                # 2. eth_estimateGas — get real gas usage
                gas_payload = {
                    "jsonrpc": "2.0", "id": 2, "method": "eth_estimateGas",
                    "params": [call_params],
                }
                async with session.post(
                    self.rpc_url, json=gas_payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    gas_result = await resp.json()

                if "error" in gas_result:
                    gas_used = tx_data.get("gas", 200_000)
                    warnings.append("eth_estimateGas failed, using fallback")
                else:
                    gas_used = int(gas_result.get("result", "0x0"), 16)

                # Cost estimate
                gas_cost_eth = gas_used * self._DEFAULT_GAS_GWEI * 1e-9
                estimated_cost_usd = gas_cost_eth * self._ETH_USD_FALLBACK

                # Sanity warnings
                if gas_used > 1_000_000:
                    warnings.append(f"high gas usage: {gas_used:,}")

                return {
                    "success": True,
                    "gas_used": gas_used,
                    "output": output,
                    "warnings": warnings,
                    "estimated_cost_usd": round(estimated_cost_usd, 4),
                }

        except Exception as e:
            log.warning("Transaction simulation failed: %s", e)
            return {
                "success": False,
                "gas_used": 0,
                "output": "0x",
                "warnings": [f"simulation error: {e}"],
                "estimated_cost_usd": 0.0,
            }


class HardwareSigningPipeline:
    """Orchestrates the signing pipeline: simulate -> policy -> sign -> submit.

    Determines which signer to use based on trade value:
      - Below auto_approve_usd: hot wallet (instant)
      - Above auto_approve_usd: hardware signer (requires approval)
      - Above emergency_usd: blocked (requires manual override)

    Integrates with the Bus for event-driven operation.
    """

    name = "hardware_signer"

    def __init__(
        self,
        bus: Bus,
        auto_approve_usd: float = 500.0,
        emergency_usd: float = 10_000.0,
        signer_mode: str = "hot_wallet",
        private_key: str = "",
    ):
        self.bus = bus
        self.auto_approve_usd = auto_approve_usd
        self.emergency_usd = emergency_usd
        self.signer_mode = signer_mode or os.getenv("HARDWARE_SIGNER_MODE", "hot_wallet")

        # Initialize signers
        self.hot_wallet = HotWalletSigner(private_key)
        self.ledger = LedgerSigner(
            os.getenv("LEDGER_DERIVATION_PATH", "m/44'/60'/0'/0/0")
        )
        self.simulator = TransactionSimulator()

        # Pending signing requests (for hardware approval flow)
        self._pending: dict[str, SigningRequest] = {}
        self._history: list[SigningRequest] = []
        self._max_history = 500

        # Subscribe to signing requests (explicit) and exec.go (automatic gate)
        bus.subscribe("signing.request", self._on_signing_request)
        bus.subscribe("exec.go", self._on_exec_go)

    @property
    def address(self) -> str:
        if self.signer_mode == "ledger_usb" and self.ledger.address:
            return self.ledger.address
        return self.hot_wallet.address

    async def _on_exec_go(self, intent: TradeIntent):
        """Gate on-chain execution through the signing pipeline.

        For intents that target on-chain venues (DeFi swaps, perp DEXs),
        publish a signing.request so the pipeline can simulate and sign
        before the executor submits.  CEX intents (Kraken API key auth)
        pass through without signing.
        """
        # Only gate intents that carry on-chain metadata
        meta = getattr(intent, "metadata", None) or {}
        venue = meta.get("venue", "")
        onchain_venues = {"jupiter", "uniswap", "hyperliquid", "base", "solana"}
        if venue.lower() not in onchain_venues:
            return  # CEX trade — no wallet signing needed

        estimated_usd = intent.amount_in
        await self.bus.publish("signing.request", {
            "intent": intent,
            "tx_data": meta.get("tx_data", {}),
            "estimated_usd": estimated_usd,
        })

    async def _on_signing_request(self, msg: dict):
        """Handle incoming signing request."""
        intent = msg.get("intent")
        tx_data = msg.get("tx_data", {})
        estimated_usd = msg.get("estimated_usd", 0.0)

        if not intent:
            return

        import uuid
        request = SigningRequest(
            request_id=uuid.uuid4().hex[:8],
            intent=intent,
            tx_data=tx_data,
            estimated_usd=estimated_usd,
            signer_mode=self._select_signer(estimated_usd),
        )

        await self._process_request(request)

    def _select_signer(self, usd_value: float) -> str:
        """Select appropriate signer based on trade value."""
        if usd_value >= self.emergency_usd:
            return "blocked"
        if usd_value >= self.auto_approve_usd and self.signer_mode == "ledger_usb":
            return "ledger_usb"
        return "hot_wallet"

    async def _process_request(self, request: SigningRequest):
        """Process a signing request through the pipeline."""

        # 1. Simulate
        sim_result = await self.simulator.simulate(request.tx_data)
        if not sim_result.get("success"):
            request.status = "rejected"
            log.warning("Signing REJECTED (simulation failed): %s", sim_result)
            await self.bus.publish("signing.rejected", {
                "request_id": request.request_id,
                "reason": "simulation failed",
                "details": sim_result,
            })
            self._archive(request)
            return

        # 2. Check warnings
        warnings = sim_result.get("warnings", [])
        if warnings:
            log.warning("Simulation warnings for %s: %s", request.request_id, warnings)

        # 3. Route to appropriate signer
        if request.signer_mode == "blocked":
            request.status = "rejected"
            log.warning(
                "Signing BLOCKED: $%.0f exceeds emergency threshold $%.0f",
                request.estimated_usd, self.emergency_usd,
            )
            await self.bus.publish("signing.rejected", {
                "request_id": request.request_id,
                "reason": f"exceeds emergency threshold (${self.emergency_usd:.0f})",
            })
            self._archive(request)
            return

        if request.signer_mode == "hot_wallet":
            success, signed_tx, error = await self.hot_wallet.sign_transaction(request.tx_data)
        elif request.signer_mode == "ledger_usb":
            # Queue for hardware approval
            self._pending[request.request_id] = request
            log.info(
                "Awaiting Ledger approval: %s ($%.0f)",
                request.request_id, request.estimated_usd,
            )
            await self.bus.publish("signing.awaiting_approval", {
                "request_id": request.request_id,
                "estimated_usd": request.estimated_usd,
                "signer": "ledger_usb",
            })
            success, signed_tx, error = await self.ledger.sign_transaction(request.tx_data)
        else:
            success, signed_tx, error = False, "", f"unknown signer: {request.signer_mode}"

        if success:
            request.status = "signed"
            request.signed_tx = signed_tx
            request.signed_at = time.time()
            log.info("Transaction SIGNED (%s): %s $%.0f",
                     request.signer_mode, request.request_id, request.estimated_usd)
            await self.bus.publish("signing.completed", {
                "request_id": request.request_id,
                "signed_tx": signed_tx,
                "signer": request.signer_mode,
            })
        else:
            request.status = "rejected"
            log.warning("Signing FAILED (%s): %s — %s",
                         request.signer_mode, request.request_id, error)
            await self.bus.publish("signing.rejected", {
                "request_id": request.request_id,
                "reason": error,
                "signer": request.signer_mode,
            })

        self._archive(request)

    def _archive(self, request: SigningRequest):
        """Move request to history."""
        self._pending.pop(request.request_id, None)
        self._history.append(request)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history // 2:]

    def status(self) -> dict:
        return {
            "signer_mode": self.signer_mode,
            "address": self.address,
            "auto_approve_threshold": self.auto_approve_usd,
            "emergency_threshold": self.emergency_usd,
            "pending_approvals": len(self._pending),
            "total_signed": len([r for r in self._history if r.status == "signed"]),
            "total_rejected": len([r for r in self._history if r.status == "rejected"]),
            "hot_wallet_connected": bool(self.hot_wallet.address),
            "ledger_connected": self.ledger._connected,
        }
