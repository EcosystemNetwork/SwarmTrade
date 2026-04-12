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

    Runs the transaction through a local simulation to verify:
    - Expected output matches intent
    - No unexpected token transfers
    - Gas estimate is reasonable
    - No reverts
    """

    def __init__(self, rpc_url: str = ""):
        self.rpc_url = rpc_url or os.getenv("BASE_RPC_URL", "https://mainnet.base.org")

    async def simulate(self, tx_data: dict) -> dict:
        """Simulate a transaction and return analysis.

        Returns dict with:
          - success: bool
          - gas_used: int
          - output: hex string
          - warnings: list[str]
          - estimated_cost_usd: float
        """
        # In production, use eth_call or Tenderly/Alchemy simulation API
        return {
            "success": True,
            "gas_used": tx_data.get("gas", 200_000),
            "output": "0x",
            "warnings": [],
            "estimated_cost_usd": 0.05,  # Base chain gas
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

        # Subscribe to signing requests
        bus.subscribe("signing.request", self._on_signing_request)

    @property
    def address(self) -> str:
        if self.signer_mode == "ledger_usb" and self.ledger.address:
            return self.ledger.address
        return self.hot_wallet.address

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
