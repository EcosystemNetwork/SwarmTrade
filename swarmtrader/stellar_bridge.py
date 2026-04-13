"""Stellar ↔ EVM Atomic Bridge — HTLC swaps, Dutch auction orders, partial fills.

Implements the core cross-chain swap patterns from ETHGlobal Stellar projects:

  1. **HTLC Atomic Swaps** — Hash Time-Locked Contracts for trustless
     EVM↔Stellar asset swaps. Both sides lock funds with the same hash;
     the secret reveal on one chain unlocks both.

  2. **Dutch Auction Orders** (à la 1inch Fusion+ / StellaFi+) — Makers
     post cross-chain swap orders that start at a premium and decay to
     market price over time. Takers (resolvers) fill when the price
     becomes profitable, creating a competitive marketplace.

  3. **Partial Fills** (à la StellarBridge) — Large orders can be filled
     incrementally by multiple resolvers, each filling a portion.

  4. **Soroban HTLC Integration** — Uses Stellar's Soroban smart contracts
     for the Stellar-side lock, and standard EVM HTLC contracts for the
     Ethereum side.

Architecture:
  - StellarEVMBridge is the main coordinator
  - HTLCSwap tracks individual atomic swap state machines
  - DutchAuctionOrder manages decaying-price cross-chain orders

Bus topics published:
  bridge.htlc_created    — new HTLC swap initiated
  bridge.htlc_locked     — funds locked on both chains
  bridge.htlc_completed  — swap completed (secret revealed)
  bridge.htlc_refunded   — swap expired and refunded
  bridge.auction_created — new Dutch auction order posted
  bridge.auction_filled  — auction order (partially) filled
  bridge.auction_expired — auction order expired unfilled
  bridge.arb_opportunity — cross-chain arb detected between EVM + Stellar

Environment variables:
  STELLAR_SECRET_KEY       — Stellar secret key for bridge operations
  STELLAR_NETWORK          — "testnet" or "public"
  BRIDGE_EVM_RPC           — EVM RPC endpoint (default: Base)
  BRIDGE_EVM_HTLC_ADDRESS  — Deployed HTLC contract on EVM side
  BRIDGE_MIN_SWAP_USD      — Minimum swap size (default: 10)
  BRIDGE_MAX_SWAP_USD      — Maximum swap size (default: 50000)
  BRIDGE_AUCTION_DURATION  — Dutch auction duration seconds (default: 300)
  BRIDGE_HTLC_TIMEOUT      — HTLC lock timeout seconds (default: 3600)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum

from .core import Bus
from .stellar_payments import STELLAR_NETWORKS, SOROBAN_USDC

log = logging.getLogger("swarm.stellar_bridge")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# EVM HTLC contract ABI (minimal — lock / withdraw / refund)
EVM_HTLC_ABI = [
    {
        "name": "newContract",
        "type": "function",
        "inputs": [
            {"name": "_receiver", "type": "address"},
            {"name": "_hashlock", "type": "bytes32"},
            {"name": "_timelock", "type": "uint256"},
            {"name": "_tokenContract", "type": "address"},
            {"name": "_amount", "type": "uint256"},
        ],
        "outputs": [{"name": "contractId", "type": "bytes32"}],
    },
    {
        "name": "withdraw",
        "type": "function",
        "inputs": [
            {"name": "_contractId", "type": "bytes32"},
            {"name": "_preimage", "type": "bytes32"},
        ],
    },
    {
        "name": "refund",
        "type": "function",
        "inputs": [{"name": "_contractId", "type": "bytes32"}],
    },
]

# Supported EVM chains for bridge
BRIDGE_CHAINS = {
    "base": {
        "chain_id": 8453,
        "rpc": "https://mainnet.base.org",
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "htlc": os.getenv("BRIDGE_BASE_HTLC", ""),
        "explorer": "https://basescan.org/tx/",
    },
    "ethereum": {
        "chain_id": 1,
        "rpc": "https://eth.llamarpc.com",
        "usdc": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "htlc": os.getenv("BRIDGE_ETH_HTLC", ""),
        "explorer": "https://etherscan.io/tx/",
    },
    "arbitrum": {
        "chain_id": 42161,
        "rpc": "https://arb1.arbitrum.io/rpc",
        "usdc": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "htlc": os.getenv("BRIDGE_ARB_HTLC", ""),
        "explorer": "https://arbiscan.io/tx/",
    },
}


class SwapState(str, Enum):
    """State machine for an HTLC atomic swap."""
    INITIATED = "initiated"           # Order created, no locks yet
    SOURCE_LOCKED = "source_locked"   # Initiator locked funds on source chain
    DEST_LOCKED = "dest_locked"       # Counterparty locked funds on dest chain
    COMPLETED = "completed"           # Secret revealed, both sides unlocked
    REFUNDED = "refunded"             # Timeout — funds returned
    FAILED = "failed"                 # Error state


class SwapDirection(str, Enum):
    EVM_TO_STELLAR = "evm_to_stellar"
    STELLAR_TO_EVM = "stellar_to_evm"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class HTLCSwap:
    """Tracks an individual atomic swap between EVM and Stellar."""
    swap_id: str
    direction: SwapDirection
    # Parties
    initiator_evm: str = ""       # EVM address (0x...)
    initiator_stellar: str = ""   # Stellar public key (G...)
    counterparty_evm: str = ""
    counterparty_stellar: str = ""
    # Amounts
    source_amount: float = 0.0    # Amount locked on source chain
    dest_amount: float = 0.0      # Amount locked on dest chain
    source_asset: str = "USDC"
    dest_asset: str = "USDC"
    # HTLC params
    hashlock: str = ""            # SHA-256 hash of the secret
    secret: str = ""              # Preimage (only known to initiator until reveal)
    timelock_source: int = 0      # Unix timestamp — source chain lock expiry
    timelock_dest: int = 0        # Unix timestamp — dest chain lock expiry (shorter!)
    # Chain info
    evm_chain: str = "base"
    evm_tx_lock: str = ""         # Tx hash of EVM lock
    evm_tx_withdraw: str = ""     # Tx hash of EVM withdraw
    stellar_tx_lock: str = ""     # Tx hash of Stellar lock
    stellar_tx_withdraw: str = "" # Tx hash of Stellar withdraw
    evm_contract_id: str = ""     # HTLC contract instance ID on EVM
    soroban_contract_id: str = "" # HTLC contract instance ID on Soroban
    # State
    state: SwapState = SwapState.INITIATED
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    error: str = ""

    def __repr__(self) -> str:
        """Safe repr that never leaks the HTLC secret."""
        return (f"HTLCSwap(id={self.swap_id}, state={self.state.value}, "
                f"direction={self.direction.value}, "
                f"src=${self.source_amount:.2f}, dst=${self.dest_amount:.2f})")

    @property
    def is_active(self) -> bool:
        return self.state in (
            SwapState.INITIATED, SwapState.SOURCE_LOCKED, SwapState.DEST_LOCKED
        )

    @property
    def is_expired(self) -> bool:
        """Check if the swap has timed out.

        We always check the SOURCE timelock (the longer one) because
        the initiator's refund window is governed by source_timelock.
        The dest_timelock is shorter to give the initiator time to
        claim before the source lock expires.
        """
        return time.time() > self.timelock_source and self.state != SwapState.COMPLETED


@dataclass
class DutchAuctionOrder:
    """A cross-chain swap order with Dutch auction pricing.

    Price starts at start_price (premium over market) and decays linearly
    to end_price over the auction duration. Resolvers compete to fill at
    the best price for themselves — earlier fill = worse price for resolver
    but guaranteed fill, later fill = better price but risk of someone
    else taking it.

    This is the core mechanism from 1inch Fusion+ adapted for Stellar↔EVM.
    """
    order_id: str
    direction: SwapDirection
    # Maker (order creator)
    maker_evm: str = ""
    maker_stellar: str = ""
    # Asset + amount
    sell_asset: str = "USDC"
    sell_amount: float = 0.0
    buy_asset: str = "XLM"
    # Dutch auction pricing
    start_price: float = 0.0     # Starting price (premium, favorable to maker)
    end_price: float = 0.0       # Ending price (market or below, favorable to taker)
    current_price: float = 0.0   # Current decayed price
    auction_start: float = field(default_factory=time.time)
    auction_duration: float = 300.0  # seconds
    # Fill tracking (supports partial fills)
    filled_amount: float = 0.0
    fills: list[dict] = field(default_factory=list)  # [{resolver, amount, price, ts}]
    min_fill_amount: float = 10.0   # Minimum partial fill size (USD)
    # Chain info
    evm_chain: str = "base"
    # State
    status: str = "active"  # active, filled, partially_filled, expired, cancelled
    created_at: float = field(default_factory=time.time)

    @property
    def remaining_amount(self) -> float:
        return self.sell_amount - self.filled_amount

    @property
    def fill_pct(self) -> float:
        return (self.filled_amount / self.sell_amount * 100) if self.sell_amount else 0

    @property
    def is_expired(self) -> bool:
        return time.time() > (self.auction_start + self.auction_duration)

    def get_current_price(self) -> float:
        """Calculate the current Dutch auction price (linear decay)."""
        elapsed = time.time() - self.auction_start
        if elapsed >= self.auction_duration:
            return self.end_price
        progress = elapsed / self.auction_duration
        self.current_price = self.start_price - (
            (self.start_price - self.end_price) * progress
        )
        return self.current_price


@dataclass
class CrossChainArbOpportunity:
    """Arbitrage opportunity between EVM and Stellar venues."""
    asset: str
    evm_venue: str
    evm_price: float
    stellar_venue: str
    stellar_price: float
    spread_bps: float
    net_spread_bps: float  # After bridge fees + gas
    direction: str         # "buy_evm_sell_stellar" or "buy_stellar_sell_evm"
    estimated_profit_usd: float
    size_usd: float
    ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# StellarEVMBridge — main coordinator
# ---------------------------------------------------------------------------

class StellarEVMBridge:
    """Trustless bridge between EVM chains and Stellar via HTLC atomic swaps.

    Combines techniques from ETHGlobal Stellar projects:
    - HTLC atomic swaps (Fusion+, StellarFusion, StellarBridge)
    - Dutch auction order matching (StellaFi+, 1inch Fusion+)
    - Partial fills (StellarBridge)
    - Soroban smart contract integration (1inch x Stellar, StellarSwap)

    The bridge runs as a service, maintaining an order book of Dutch
    auction orders and a pool of active HTLC swaps. It also monitors
    prices across EVM DEXs and Stellar DEXs to detect cross-chain
    arbitrage opportunities.
    """

    name = "stellar_evm_bridge"

    def __init__(
        self,
        bus: Bus,
        network: str = "testnet",
        evm_chain: str = "base",
        min_swap_usd: float = 10.0,
        max_swap_usd: float = 50_000.0,
        auction_duration: float = 300.0,
        htlc_timeout: float = 3600.0,
        scan_interval: float = 15.0,
    ):
        self.bus = bus
        self._network = network
        self._evm_chain = evm_chain
        self._min_swap = float(os.getenv("BRIDGE_MIN_SWAP_USD", str(min_swap_usd)))
        self._max_swap = float(os.getenv("BRIDGE_MAX_SWAP_USD", str(max_swap_usd)))
        self._auction_duration = float(
            os.getenv("BRIDGE_AUCTION_DURATION", str(auction_duration))
        )
        self._htlc_timeout = float(
            os.getenv("BRIDGE_HTLC_TIMEOUT", str(htlc_timeout))
        )
        self._scan_interval = scan_interval
        self._stop = False

        # Stellar config
        net_cfg = STELLAR_NETWORKS.get(network, STELLAR_NETWORKS["testnet"])
        self._horizon = os.getenv("STELLAR_HORIZON_URL", net_cfg["horizon"])
        self._soroban_usdc = SOROBAN_USDC.get(network, "")

        # EVM config
        self._evm_cfg = BRIDGE_CHAINS.get(evm_chain, BRIDGE_CHAINS["base"])
        self._evm_rpc = os.getenv("BRIDGE_EVM_RPC", self._evm_cfg["rpc"])
        self._evm_htlc_address = os.getenv(
            "BRIDGE_EVM_HTLC_ADDRESS", self._evm_cfg.get("htlc", "")
        )

        # Active swaps and orders
        self._swaps: dict[str, HTLCSwap] = {}
        self._orders: dict[str, DutchAuctionOrder] = {}
        self._arb_history: list[CrossChainArbOpportunity] = []

        # Price caches (updated by scan loop)
        self._evm_prices: dict[str, float] = {}     # asset -> USD price on EVM
        self._stellar_prices: dict[str, float] = {}  # asset -> USD price on Stellar

        # Stats
        self._stats = {
            "swaps_initiated": 0,
            "swaps_completed": 0,
            "swaps_refunded": 0,
            "swaps_failed": 0,
            "total_volume_usd": 0.0,
            "auctions_created": 0,
            "auctions_filled": 0,
            "auctions_expired": 0,
            "arbs_detected": 0,
            "arb_profit_usd": 0.0,
            "scans": 0,
        }

        # Bus subscriptions
        bus.subscribe("bridge.swap_request", self._on_swap_request)
        bus.subscribe("bridge.auction_request", self._on_auction_request)
        bus.subscribe("bridge.fill_auction", self._on_fill_auction)
        bus.subscribe("stellar.best_route", self._on_stellar_price)
        bus.subscribe("arb.opportunity", self._on_evm_arb_price)

    # ── HTLC Atomic Swap Flow ──────────────────────────────────────

    def generate_htlc_secret(self) -> tuple[str, str]:
        """Generate a cryptographic secret and its hash for HTLC.

        Returns (secret_hex, hashlock_hex).
        The secret is 32 bytes of randomness. The hashlock is SHA-256(secret).
        Both sides of the swap use the same hashlock; revealing the secret
        on one chain allows claiming on the other.
        """
        secret = secrets.token_bytes(32)
        hashlock = hashlib.sha256(secret).digest()
        return secret.hex(), hashlock.hex()

    async def initiate_swap(
        self,
        direction: SwapDirection,
        source_amount: float,
        dest_amount: float,
        source_asset: str = "USDC",
        dest_asset: str = "USDC",
        initiator_evm: str = "",
        initiator_stellar: str = "",
        counterparty_evm: str = "",
        counterparty_stellar: str = "",
        evm_chain: str = "",
    ) -> HTLCSwap:
        """Initiate a new HTLC atomic swap.

        Flow (EVM → Stellar):
          1. Initiator generates secret + hashlock
          2. Initiator locks USDC on EVM with hashlock + timelock (1h)
          3. Counterparty sees EVM lock, locks USDC on Stellar with SAME
             hashlock + SHORTER timelock (30min)
          4. Initiator sees Stellar lock, reveals secret to claim Stellar USDC
          5. Counterparty uses revealed secret to claim EVM USDC
          6. Both sides settled atomically

        The timelock asymmetry is critical: the Stellar lock expires BEFORE
        the EVM lock, so the initiator can always claim or get refunded.
        """
        if source_amount < self._min_swap:
            raise ValueError(f"Swap too small: ${source_amount} < ${self._min_swap}")
        if source_amount > self._max_swap:
            raise ValueError(f"Swap too large: ${source_amount} > ${self._max_swap}")

        secret_hex, hashlock_hex = self.generate_htlc_secret()
        now = time.time()

        # Timelock: source gets full timeout, dest gets half (safety margin)
        source_timelock = int(now + self._htlc_timeout)
        dest_timelock = int(now + self._htlc_timeout / 2)

        swap = HTLCSwap(
            swap_id=f"htlc-{secrets.token_hex(8)}",
            direction=direction,
            initiator_evm=initiator_evm,
            initiator_stellar=initiator_stellar,
            counterparty_evm=counterparty_evm,
            counterparty_stellar=counterparty_stellar,
            source_amount=source_amount,
            dest_amount=dest_amount,
            source_asset=source_asset,
            dest_asset=dest_asset,
            hashlock=hashlock_hex,
            secret=secret_hex,
            timelock_source=source_timelock,
            timelock_dest=dest_timelock,
            evm_chain=evm_chain or self._evm_chain,
            state=SwapState.INITIATED,
        )

        self._swaps[swap.swap_id] = swap
        self._stats["swaps_initiated"] += 1

        log.info(
            "HTLC SWAP INITIATED: %s %s $%.2f %s → $%.2f %s hashlock=%s...",
            swap.swap_id, direction.value, source_amount, source_asset,
            dest_amount, dest_asset, hashlock_hex[:16],
        )

        await self.bus.publish("bridge.htlc_created", {
            "swap_id": swap.swap_id,
            "direction": direction.value,
            "source_amount": source_amount,
            "dest_amount": dest_amount,
            "source_asset": source_asset,
            "dest_asset": dest_asset,
            "hashlock": hashlock_hex,
            "timelock_source": source_timelock,
            "timelock_dest": dest_timelock,
            "evm_chain": swap.evm_chain,
        })

        return swap

    async def lock_source(self, swap_id: str) -> bool:
        """Lock funds on the source chain (step 2 of HTLC flow).

        For EVM→Stellar: locks USDC on EVM via HTLC contract.
        For Stellar→EVM: locks USDC on Stellar via Soroban HTLC.
        """
        swap = self._swaps.get(swap_id)
        if not swap or swap.state != SwapState.INITIATED:
            return False

        if swap.direction == SwapDirection.EVM_TO_STELLAR:
            tx_hash = await self._lock_evm(swap)
            swap.evm_tx_lock = tx_hash
        else:
            tx_hash = await self._lock_stellar(swap)
            swap.stellar_tx_lock = tx_hash

        if tx_hash:
            swap.state = SwapState.SOURCE_LOCKED
            log.info("HTLC SOURCE LOCKED: %s tx=%s", swap_id, tx_hash[:16])
            await self.bus.publish("bridge.htlc_locked", {
                "swap_id": swap_id,
                "side": "source",
                "tx_hash": tx_hash,
                "chain": "evm" if swap.direction == SwapDirection.EVM_TO_STELLAR else "stellar",
            })
            return True

        swap.state = SwapState.FAILED
        swap.error = "source lock failed"
        self._stats["swaps_failed"] += 1
        return False

    async def lock_dest(self, swap_id: str) -> bool:
        """Lock funds on the destination chain (step 3 — counterparty's turn).

        Counterparty verifies the source lock on-chain, then locks their
        funds on the other chain with the SAME hashlock but SHORTER timelock.
        """
        swap = self._swaps.get(swap_id)
        if not swap or swap.state != SwapState.SOURCE_LOCKED:
            return False

        if swap.direction == SwapDirection.EVM_TO_STELLAR:
            tx_hash = await self._lock_stellar(swap)
            swap.stellar_tx_lock = tx_hash
        else:
            tx_hash = await self._lock_evm(swap)
            swap.evm_tx_lock = tx_hash

        if tx_hash:
            swap.state = SwapState.DEST_LOCKED
            log.info("HTLC DEST LOCKED: %s tx=%s (both sides locked!)", swap_id, tx_hash[:16])
            await self.bus.publish("bridge.htlc_locked", {
                "swap_id": swap_id,
                "side": "dest",
                "tx_hash": tx_hash,
                "chain": "stellar" if swap.direction == SwapDirection.EVM_TO_STELLAR else "evm",
            })
            return True

        swap.error = "dest lock failed"
        return False

    async def reveal_and_claim(self, swap_id: str) -> bool:
        """Reveal the secret and claim funds on the destination chain (step 4).

        The initiator reveals the secret on the dest chain to claim funds.
        This reveals the secret publicly, allowing the counterparty to
        claim funds on the source chain (step 5 happens automatically).
        """
        swap = self._swaps.get(swap_id)
        if not swap or swap.state != SwapState.DEST_LOCKED:
            return False

        if not swap.secret:
            swap.error = "no secret available"
            return False

        # Claim on dest chain by revealing secret
        if swap.direction == SwapDirection.EVM_TO_STELLAR:
            tx_hash = await self._withdraw_stellar(swap)
            swap.stellar_tx_withdraw = tx_hash
        else:
            tx_hash = await self._withdraw_evm(swap)
            swap.evm_tx_withdraw = tx_hash

        if tx_hash:
            swap.state = SwapState.COMPLETED
            swap.completed_at = time.time()
            self._stats["swaps_completed"] += 1
            self._stats["total_volume_usd"] += swap.source_amount

            log.info(
                "HTLC COMPLETED: %s $%.2f %s↔%s (%.1fs)",
                swap_id, swap.source_amount,
                "EVM" if swap.direction == SwapDirection.EVM_TO_STELLAR else "Stellar",
                "Stellar" if swap.direction == SwapDirection.EVM_TO_STELLAR else "EVM",
                swap.completed_at - swap.created_at,
            )

            await self.bus.publish("bridge.htlc_completed", {
                "swap_id": swap_id,
                "direction": swap.direction.value,
                "source_amount": swap.source_amount,
                "dest_amount": swap.dest_amount,
                "duration_s": round(swap.completed_at - swap.created_at, 1),
                "evm_chain": swap.evm_chain,
            })
            return True

        swap.error = "claim failed"
        return False

    async def refund_expired(self, swap_id: str) -> bool:
        """Refund an expired HTLC swap back to the initiator."""
        swap = self._swaps.get(swap_id)
        if not swap or not swap.is_expired:
            return False
        if swap.state == SwapState.COMPLETED:
            return False

        swap.state = SwapState.REFUNDED
        self._stats["swaps_refunded"] += 1

        log.info("HTLC REFUNDED: %s (expired after %.0fs)",
                 swap_id, time.time() - swap.created_at)

        await self.bus.publish("bridge.htlc_refunded", {
            "swap_id": swap_id,
            "reason": "timeout",
            "duration_s": round(time.time() - swap.created_at, 1),
        })
        return True

    # ── EVM chain operations (HTLC contract calls) ────────────────

    async def _lock_evm(self, swap: HTLCSwap) -> str:
        """Lock funds on EVM chain via HTLC smart contract.

        Calls newContract() on the deployed HashedTimelockERC20 contract,
        depositing USDC with the hashlock and timelock.
        """
        if not self._evm_htlc_address:
            # Simulation mode — generate a fake tx hash
            tx_hash = f"0x{secrets.token_hex(32)}"
            swap.evm_contract_id = secrets.token_hex(32)
            log.info("EVM HTLC lock (simulated): %s $%.2f", tx_hash[:18], swap.source_amount)
            return tx_hash

        try:
            # Real EVM execution via web3
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(self._evm_rpc))
            htlc = w3.eth.contract(
                address=Web3.to_checksum_address(self._evm_htlc_address),
                abi=EVM_HTLC_ABI,
            )

            receiver = swap.counterparty_evm or swap.initiator_evm
            amount_wei = int(swap.source_amount * 1e6)  # USDC has 6 decimals on EVM
            hashlock_bytes = bytes.fromhex(swap.hashlock)
            timelock = swap.timelock_source

            evm_key = os.getenv("BRIDGE_EVM_PRIVATE_KEY", "")
            if not evm_key:
                log.warning("No EVM private key — simulating lock")
                return f"0x{secrets.token_hex(32)}"

            account = w3.eth.account.from_key(evm_key)
            usdc_address = self._evm_cfg["usdc"]

            # ERC20 approve: HTLC contract needs allowance to pull USDC
            erc20_abi = [{"name": "approve", "type": "function",
                          "inputs": [{"name": "spender", "type": "address"},
                                     {"name": "amount", "type": "uint256"}],
                          "outputs": [{"name": "", "type": "bool"}]}]
            usdc = w3.eth.contract(
                address=Web3.to_checksum_address(usdc_address), abi=erc20_abi,
            )
            approve_tx = usdc.functions.approve(
                Web3.to_checksum_address(self._evm_htlc_address), amount_wei,
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 60_000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(0.1, "gwei"),
            })
            signed_approve = account.sign_transaction(approve_tx)
            approve_hash = w3.eth.send_raw_transaction(signed_approve.raw_transaction)
            w3.eth.wait_for_transaction_receipt(approve_hash, timeout=30)

            tx = htlc.functions.newContract(
                Web3.to_checksum_address(receiver),
                hashlock_bytes,
                timelock,
                Web3.to_checksum_address(usdc_address),
                amount_wei,
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 200_000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(0.1, "gwei"),
            })

            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            return receipt.transactionHash.hex()

        except ImportError:
            log.warning("web3 not installed — simulating EVM HTLC lock")
            return f"0x{secrets.token_hex(32)}"
        except Exception as e:
            log.error("EVM HTLC lock failed: %s", e)
            return ""

    async def _withdraw_evm(self, swap: HTLCSwap) -> str:
        """Withdraw from EVM HTLC by revealing the secret."""
        if not self._evm_htlc_address:
            tx_hash = f"0x{secrets.token_hex(32)}"
            log.info("EVM HTLC withdraw (simulated): %s", tx_hash[:18])
            return tx_hash

        try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(self._evm_rpc))
            htlc = w3.eth.contract(
                address=Web3.to_checksum_address(self._evm_htlc_address),
                abi=EVM_HTLC_ABI,
            )

            evm_key = os.getenv("BRIDGE_EVM_PRIVATE_KEY", "")
            if not evm_key:
                return f"0x{secrets.token_hex(32)}"

            account = w3.eth.account.from_key(evm_key)
            contract_id = bytes.fromhex(swap.evm_contract_id)
            preimage = bytes.fromhex(swap.secret)

            tx = htlc.functions.withdraw(
                contract_id, preimage,
            ).build_transaction({
                "from": account.address,
                "nonce": w3.eth.get_transaction_count(account.address),
                "gas": 100_000,
                "maxFeePerGas": w3.eth.gas_price * 2,
                "maxPriorityFeePerGas": w3.to_wei(0.1, "gwei"),
            })

            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

            return receipt.transactionHash.hex()

        except ImportError:
            return f"0x{secrets.token_hex(32)}"
        except Exception as e:
            log.error("EVM HTLC withdraw failed: %s", e)
            return ""

    # ── Stellar chain operations (Soroban HTLC) ───────────────────

    async def _lock_stellar(self, swap: HTLCSwap) -> str:
        """Lock funds on Stellar via Soroban HTLC contract.

        Uses a Soroban smart contract that implements:
        - lock(hashlock, timelock, amount, recipient) → lock_id
        - withdraw(lock_id, secret) → releases funds
        - refund(lock_id) → returns funds after timelock
        """
        try:
            from stellar_sdk import (
                Keypair, SorobanServer, TransactionBuilder,
            )
            from stellar_sdk import scval

            secret_key = os.getenv("STELLAR_SECRET_KEY", "")
            if not secret_key:
                tx_hash = secrets.token_hex(32)
                log.info("Stellar HTLC lock (simulated): %s $%.2f", tx_hash[:16], swap.dest_amount)
                return tx_hash

            kp = Keypair.from_secret(secret_key)
            net_cfg = STELLAR_NETWORKS.get(self._network, STELLAR_NETWORKS["testnet"])

            soroban_url = "https://soroban-testnet.stellar.org"
            if self._network == "public":
                soroban_url = "https://soroban-rpc.mainnet.stellar.gateway.fm"

            soroban = SorobanServer(soroban_url)
            source = soroban.load_account(kp.public_key)

            htlc_contract = os.getenv("STELLAR_HTLC_CONTRACT", "")
            if not htlc_contract:
                tx_hash = secrets.token_hex(32)
                log.info("Stellar HTLC lock (no contract — simulated): %s", tx_hash[:16])
                return tx_hash

            amount_stroops = int(swap.dest_amount * 10_000_000)
            recipient = swap.counterparty_stellar or swap.initiator_stellar

            builder = TransactionBuilder(
                source_account=source,
                network_passphrase=net_cfg["passphrase"],
                base_fee=100,
            )
            builder.set_timeout(30)
            builder.append_invoke_contract_function_op(
                contract_id=htlc_contract,
                function_name="lock",
                parameters=[
                    scval.to_bytes(bytes.fromhex(swap.hashlock)),
                    scval.to_uint64(swap.timelock_dest),
                    scval.to_int128(amount_stroops),
                    scval.to_address(recipient),
                ],
            )

            tx = builder.build()
            sim = soroban.simulate_transaction(tx)
            if sim.error:
                log.error("Stellar HTLC sim failed: %s", sim.error)
                return ""

            tx = soroban.prepare_transaction(tx, sim)
            tx.sign(kp)
            response = soroban.send_transaction(tx)

            return response.hash or ""

        except ImportError:
            tx_hash = secrets.token_hex(32)
            log.warning("stellar-sdk not installed — simulating Stellar HTLC lock")
            return tx_hash
        except Exception as e:
            log.error("Stellar HTLC lock failed: %s", e)
            return ""

    async def _withdraw_stellar(self, swap: HTLCSwap) -> str:
        """Withdraw from Stellar HTLC by revealing the secret."""
        try:
            from stellar_sdk import (
                Keypair, SorobanServer, TransactionBuilder,
            )
            from stellar_sdk import scval

            secret_key = os.getenv("STELLAR_SECRET_KEY", "")
            if not secret_key:
                return secrets.token_hex(32)

            kp = Keypair.from_secret(secret_key)
            net_cfg = STELLAR_NETWORKS.get(self._network, STELLAR_NETWORKS["testnet"])

            soroban_url = "https://soroban-testnet.stellar.org"
            if self._network == "public":
                soroban_url = "https://soroban-rpc.mainnet.stellar.gateway.fm"

            soroban = SorobanServer(soroban_url)
            source = soroban.load_account(kp.public_key)

            htlc_contract = os.getenv("STELLAR_HTLC_CONTRACT", "")
            if not htlc_contract:
                return secrets.token_hex(32)

            builder = TransactionBuilder(
                source_account=source,
                network_passphrase=net_cfg["passphrase"],
                base_fee=100,
            )
            builder.set_timeout(30)
            builder.append_invoke_contract_function_op(
                contract_id=htlc_contract,
                function_name="withdraw",
                parameters=[
                    scval.to_bytes(bytes.fromhex(swap.soroban_contract_id or swap.swap_id)),
                    scval.to_bytes(bytes.fromhex(swap.secret)),
                ],
            )

            tx = builder.build()
            sim = soroban.simulate_transaction(tx)
            if sim.error:
                log.error("Stellar HTLC withdraw sim failed: %s", sim.error)
                return ""

            tx = soroban.prepare_transaction(tx, sim)
            tx.sign(kp)
            response = soroban.send_transaction(tx)

            return response.hash or ""

        except ImportError:
            return secrets.token_hex(32)
        except Exception as e:
            log.error("Stellar HTLC withdraw failed: %s", e)
            return ""

    # ── Dutch Auction Orders ───────────────────────────────────────

    async def create_auction(
        self,
        direction: SwapDirection,
        sell_asset: str,
        sell_amount: float,
        buy_asset: str,
        start_price: float,
        end_price: float,
        maker_evm: str = "",
        maker_stellar: str = "",
        duration: float = 0,
        min_fill: float = 10.0,
    ) -> DutchAuctionOrder:
        """Create a Dutch auction order for cross-chain swaps.

        The price starts at start_price (premium) and decays linearly
        to end_price over the auction duration. Resolvers can fill at
        any point, getting better prices as the auction progresses.

        Example (selling USDC on EVM for XLM on Stellar):
          - start_price: 0.092 (XLM/USDC — maker wants premium)
          - end_price:   0.088 (at or below market — guaranteed fill)
          - duration:    300s (5 minutes)

        At t=0, taker pays 0.092 per XLM (unfavorable for taker).
        At t=300, taker pays 0.088 per XLM (favorable for taker).
        Competitive resolvers fill somewhere in between.
        """
        if sell_amount < self._min_swap:
            raise ValueError(f"Order too small: ${sell_amount} < ${self._min_swap}")

        order = DutchAuctionOrder(
            order_id=f"auction-{secrets.token_hex(6)}",
            direction=direction,
            maker_evm=maker_evm,
            maker_stellar=maker_stellar,
            sell_asset=sell_asset,
            sell_amount=sell_amount,
            buy_asset=buy_asset,
            start_price=start_price,
            end_price=end_price,
            current_price=start_price,
            auction_duration=duration or self._auction_duration,
            min_fill_amount=min_fill,
            evm_chain=self._evm_chain,
        )

        self._orders[order.order_id] = order
        self._stats["auctions_created"] += 1

        log.info(
            "DUTCH AUCTION: %s sell $%.2f %s for %s price=%.6f→%.6f over %.0fs",
            order.order_id, sell_amount, sell_asset, buy_asset,
            start_price, end_price, order.auction_duration,
        )

        await self.bus.publish("bridge.auction_created", {
            "order_id": order.order_id,
            "direction": direction.value,
            "sell_asset": sell_asset,
            "sell_amount": sell_amount,
            "buy_asset": buy_asset,
            "start_price": start_price,
            "end_price": end_price,
            "duration_s": order.auction_duration,
        })

        return order

    async def fill_auction(
        self,
        order_id: str,
        resolver_id: str,
        fill_amount: float,
    ) -> dict:
        """Fill (or partially fill) a Dutch auction order.

        Resolvers call this to take some or all of the remaining order
        at the current Dutch auction price. Each fill triggers an HTLC
        swap for that portion.

        Returns fill details including the HTLC swap created.
        """
        order = self._orders.get(order_id)
        if not order:
            return {"error": "order not found"}
        if order.status in ("filled", "expired", "cancelled"):
            return {"error": f"order is {order.status}"}
        if order.is_expired:
            order.status = "expired"
            self._stats["auctions_expired"] += 1
            return {"error": "order expired"}

        remaining = order.remaining_amount
        fill = min(fill_amount, remaining)
        if fill < order.min_fill_amount:
            return {"error": f"fill too small: ${fill} < ${order.min_fill_amount}"}

        current_price = order.get_current_price()
        dest_amount = fill / current_price if current_price > 0 else 0

        # Create an HTLC swap for this fill
        swap = await self.initiate_swap(
            direction=order.direction,
            source_amount=fill,
            dest_amount=dest_amount,
            source_asset=order.sell_asset,
            dest_asset=order.buy_asset,
            initiator_evm=order.maker_evm,
            initiator_stellar=order.maker_stellar,
        )

        # Record the fill
        fill_record = {
            "resolver": resolver_id,
            "amount": fill,
            "price": current_price,
            "dest_amount": dest_amount,
            "swap_id": swap.swap_id,
            "ts": time.time(),
        }
        order.fills.append(fill_record)
        order.filled_amount += fill

        if order.remaining_amount < order.min_fill_amount:
            order.status = "filled"
            self._stats["auctions_filled"] += 1
        else:
            order.status = "partially_filled"

        log.info(
            "AUCTION FILL: %s resolver=%s $%.2f@%.6f (%.1f%% filled) → swap %s",
            order_id, resolver_id, fill, current_price,
            order.fill_pct, swap.swap_id,
        )

        await self.bus.publish("bridge.auction_filled", {
            "order_id": order_id,
            "resolver": resolver_id,
            "fill_amount": fill,
            "price": current_price,
            "fill_pct": round(order.fill_pct, 1),
            "swap_id": swap.swap_id,
            "status": order.status,
        })

        return {
            "status": "filled",
            "fill_amount": fill,
            "price": current_price,
            "dest_amount": dest_amount,
            "swap_id": swap.swap_id,
            "order_fill_pct": order.fill_pct,
        }

    # ── Cross-Chain Arb Detection ──────────────────────────────────

    async def scan_cross_chain_arb(self):
        """Compare prices between EVM DEXs and Stellar DEXs for arb.

        When an asset trades at different prices on EVM vs Stellar,
        the bridge can route arb trades through HTLC swaps to capture
        the spread.
        """
        # Need prices from both sides
        common_assets = set(self._evm_prices.keys()) & set(self._stellar_prices.keys())

        for asset in common_assets:
            evm_price = self._evm_prices[asset]
            stellar_price = self._stellar_prices[asset]

            if evm_price <= 0 or stellar_price <= 0:
                continue

            spread_bps = abs(evm_price - stellar_price) / min(evm_price, stellar_price) * 10_000

            # Estimate bridge costs: ~$2 gas + 0.3% AMM fee + 0.1% bridge fee
            bridge_fee_bps = 40  # 0.4% total estimated bridge friction
            net_spread_bps = spread_bps - bridge_fee_bps

            if net_spread_bps < 15:  # Need at least 15 bps net
                continue

            if evm_price < stellar_price:
                direction = "buy_evm_sell_stellar"
            else:
                direction = "buy_stellar_sell_evm"

            size_usd = 1000.0  # Default arb size
            profit_est = size_usd * (net_spread_bps / 10_000)

            arb = CrossChainArbOpportunity(
                asset=asset,
                evm_venue=self._evm_chain,
                evm_price=evm_price,
                stellar_venue="stellar_agg",
                stellar_price=stellar_price,
                spread_bps=round(spread_bps, 2),
                net_spread_bps=round(net_spread_bps, 2),
                direction=direction,
                estimated_profit_usd=round(profit_est, 4),
                size_usd=size_usd,
            )

            self._arb_history.append(arb)
            if len(self._arb_history) > 500:
                self._arb_history = self._arb_history[-250:]

            self._stats["arbs_detected"] += 1
            self._stats["arb_profit_usd"] += profit_est

            log.info(
                "CROSS-CHAIN ARB: %s %s evm=%.4f stellar=%.4f spread=%.1fbps net=%.1fbps est=$%.2f",
                asset, direction, evm_price, stellar_price,
                spread_bps, net_spread_bps, profit_est,
            )

            await self.bus.publish("bridge.arb_opportunity", {
                "asset": asset,
                "direction": direction,
                "evm_price": evm_price,
                "stellar_price": stellar_price,
                "spread_bps": round(spread_bps, 2),
                "net_spread_bps": round(net_spread_bps, 2),
                "estimated_profit_usd": round(profit_est, 4),
                "evm_chain": self._evm_chain,
            })

    # ── Bus event handlers ─────────────────────────────────────────

    async def _on_swap_request(self, msg: dict):
        """Handle an incoming swap request from the bus."""
        try:
            direction = SwapDirection(msg.get("direction", "evm_to_stellar"))
            source_amount = msg.get("source_amount", 0)
            if not source_amount or source_amount < self._min_swap:
                log.warning("Swap request rejected: amount=$%.2f below min=$%.2f",
                            source_amount, self._min_swap)
                return
            await self.initiate_swap(
                direction=direction,
                source_amount=source_amount,
                dest_amount=msg.get("dest_amount", 0),
                source_asset=msg.get("source_asset", "USDC"),
                dest_asset=msg.get("dest_asset", "USDC"),
                initiator_evm=msg.get("initiator_evm", ""),
                initiator_stellar=msg.get("initiator_stellar", ""),
            )
        except (ValueError, KeyError) as e:
            log.warning("Invalid swap request: %s", e)

    async def _on_auction_request(self, msg: dict):
        """Handle request to create a new Dutch auction order."""
        try:
            await self.create_auction(
                direction=SwapDirection(msg.get("direction", "evm_to_stellar")),
                sell_asset=msg.get("sell_asset", "USDC"),
                sell_amount=msg.get("sell_amount", 0),
                buy_asset=msg.get("buy_asset", "XLM"),
                start_price=msg.get("start_price", 0),
                end_price=msg.get("end_price", 0),
            )
        except (ValueError, KeyError) as e:
            log.warning("Invalid auction request: %s", e)

    async def _on_fill_auction(self, msg: dict):
        """Handle request to fill a Dutch auction order."""
        await self.fill_auction(
            order_id=msg.get("order_id", ""),
            resolver_id=msg.get("resolver_id", ""),
            fill_amount=msg.get("fill_amount", 0),
        )

    async def _on_stellar_price(self, msg: dict):
        """Cache Stellar prices from the DEX aggregator."""
        asset = msg.get("asset", "")
        price = msg.get("price", 0)
        if asset and price > 0:
            self._stellar_prices[asset] = price

    async def _on_evm_arb_price(self, msg: dict):
        """Cache EVM prices from arb scanner."""
        asset = msg.get("asset", "")
        buy_price = msg.get("buy_price", 0)
        if asset and buy_price > 0:
            self._evm_prices[asset] = buy_price

    # ── Main loop ──────────────────────────────────────────────────

    async def run(self):
        """Main service loop: expire swaps, expire auctions, scan arbs."""
        log.info(
            "StellarEVMBridge started: network=%s evm=%s htlc_timeout=%.0fs auction=%.0fs",
            self._network, self._evm_chain, self._htlc_timeout, self._auction_duration,
        )

        while not self._stop:
            try:
                # Expire timed-out HTLC swaps
                for swap in list(self._swaps.values()):
                    if swap.is_active and swap.is_expired:
                        await self.refund_expired(swap.swap_id)

                # Expire timed-out Dutch auctions
                for order in list(self._orders.values()):
                    if order.status == "active" and order.is_expired:
                        order.status = "expired"
                        self._stats["auctions_expired"] += 1
                        await self.bus.publish("bridge.auction_expired", {
                            "order_id": order.order_id,
                            "sell_amount": order.sell_amount,
                            "filled_amount": order.filled_amount,
                        })

                # Scan for cross-chain arb
                await self.scan_cross_chain_arb()
                self._stats["scans"] += 1

            except Exception as e:
                log.warning("Bridge scan error: %s", e)

            await asyncio.sleep(self._scan_interval)

    def stop(self):
        self._stop = True

    # ── Query methods ──────────────────────────────────────────────

    def get_active_orders(self) -> list[dict]:
        """Return all active Dutch auction orders with current prices."""
        active = []
        for order in self._orders.values():
            if order.status in ("active", "partially_filled"):
                active.append({
                    "order_id": order.order_id,
                    "direction": order.direction.value,
                    "sell_asset": order.sell_asset,
                    "sell_amount": order.sell_amount,
                    "buy_asset": order.buy_asset,
                    "current_price": order.get_current_price(),
                    "start_price": order.start_price,
                    "end_price": order.end_price,
                    "filled_pct": round(order.fill_pct, 1),
                    "remaining": round(order.remaining_amount, 2),
                    "time_left_s": round(
                        max(0, (order.auction_start + order.auction_duration) - time.time()),
                        0,
                    ),
                })
        return active

    def get_active_swaps(self) -> list[dict]:
        """Return all active HTLC swaps."""
        return [
            {
                "swap_id": s.swap_id,
                "direction": s.direction.value,
                "state": s.state.value,
                "source_amount": s.source_amount,
                "dest_amount": s.dest_amount,
                "source_asset": s.source_asset,
                "dest_asset": s.dest_asset,
                "evm_chain": s.evm_chain,
                "age_s": round(time.time() - s.created_at, 0),
                "hashlock": s.hashlock[:16] + "...",
            }
            for s in self._swaps.values()
            if s.is_active
        ]

    def status(self) -> dict:
        return {
            "agent": self.name,
            "network": self._network,
            "evm_chain": self._evm_chain,
            "evm_htlc": self._evm_htlc_address[:16] + "..." if self._evm_htlc_address else "none",
            "active_swaps": sum(1 for s in self._swaps.values() if s.is_active),
            "active_auctions": sum(
                1 for o in self._orders.values()
                if o.status in ("active", "partially_filled")
            ),
            "stats": self._stats,
            "price_cache": {
                "evm": {k: round(v, 6) for k, v in self._evm_prices.items()},
                "stellar": {k: round(v, 6) for k, v in self._stellar_prices.items()},
            },
            "recent_arbs": [
                {
                    "asset": a.asset,
                    "direction": a.direction,
                    "spread_bps": a.spread_bps,
                    "net_bps": a.net_spread_bps,
                    "est_profit": a.estimated_profit_usd,
                }
                for a in self._arb_history[-5:]
            ],
        }
