"""Uniswap Trading API integration for DEX execution on Base.

Provides on-chain swap execution via the Uniswap Trading API (v1),
serving as a DEX venue for the Smart Order Router and meeting the
ERC-8004 hackathon requirement for Risk Router execution.

Flow:
  1. check_approval  — ensure token spending is approved
  2. quote           — get executable quote with routing
  3. swap            — get transaction calldata to sign + submit

Environment variables:
  UNISWAP_API_KEY     — Uniswap Developer Portal API key
  PRIVATE_KEY         — wallet private key (shared with ERC-8004)
  UNISWAP_CHAIN_ID    — chain ID (default: 8453 = Base)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from .core import Bus, TradeIntent, ExecutionReport, QUOTE_ASSETS

log = logging.getLogger("swarm.uniswap")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TRADING_API_BASE = "https://trade-api.gateway.uniswap.org/v1"

# Common token addresses on Base (chain 8453)
BASE_TOKENS: dict[str, str] = {
    "ETH":  "0x0000000000000000000000000000000000000000",  # native ETH
    "WETH": "0x4200000000000000000000000000000000000006",
    "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
    "DAI":  "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
    "WBTC": "0x0555E30da8f98308EdB960aa94C0Db47230d2B9c",
    "cbETH":"0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
}

# Stablecoin addresses for direction detection
_STABLES = {"USDC", "USDT", "DAI"}

# Default slippage tolerance
DEFAULT_SLIPPAGE_BPS = 50  # 0.5%

# Router version header
ROUTER_VERSION = "2.0"


@dataclass
class UniswapQuote:
    """Parsed quote from the Trading API."""
    route_type: str          # CLASSIC, DUTCH_V2, PRIORITY, etc.
    token_in: str
    token_out: str
    amount_in: str
    amount_out: str
    gas_estimate: str
    price_impact_bps: float
    execution_price: float   # computed effective price
    raw: dict                # full API response for swap step


# ---------------------------------------------------------------------------
# UniswapClient — async wrapper around the Trading API
# ---------------------------------------------------------------------------
class UniswapClient:
    """Async client for the Uniswap Trading API."""

    def __init__(self, api_key: str, chain_id: int = 8453):
        self.api_key = api_key
        self.chain_id = chain_id
        self._session = None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            import aiohttp
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": self.api_key,
                    "x-universal-router-version": ROUTER_VERSION,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post(self, endpoint: str, payload: dict) -> dict:
        await self._ensure_session()
        url = f"{TRADING_API_BASE}/{endpoint}"
        async with self._session.post(url, json=payload) as resp:
            data = await resp.json()
            if resp.status != 200:
                detail = data.get("detail", data.get("errorCode", str(data)))
                raise RuntimeError(f"Uniswap API {endpoint} failed ({resp.status}): {detail}")
            return data

    async def check_approval(self, token_address: str, wallet: str,
                             amount: str) -> dict:
        """Check if token spending is approved for the Universal Router."""
        return await self._post("check_approval", {
            "token": token_address,
            "amount": amount,
            "chainId": self.chain_id,
            "walletAddress": wallet,
        })

    async def get_quote(self, token_in: str, token_out: str,
                        amount: str, swapper: str,
                        slippage_bps: int = DEFAULT_SLIPPAGE_BPS,
                        intent: str = "quote") -> dict:
        """Get an executable quote with routing optimization."""
        payload: dict[str, Any] = {
            "tokenInChainId": self.chain_id,
            "tokenOutChainId": self.chain_id,
            "tokenIn": token_in,
            "tokenOut": token_out,
            "amount": amount,
            "swapper": swapper,
            "slippageTolerance": slippage_bps / 10_000,
            "type": "EXACT_INPUT",
            "intent": intent,
        }
        return await self._post("quote", payload)

    async def get_swap(self, quote_response: dict) -> dict:
        """Get transaction calldata from a quote response."""
        # Strip fields that belong to the quote step only
        payload = {k: v for k, v in quote_response.items()
                   if k not in ("permitData", "permitTransaction")}
        # Re-attach permit if present
        if "permitData" in quote_response:
            payload["permitData"] = quote_response["permitData"]
        return await self._post("swap", payload)


# ---------------------------------------------------------------------------
# Token resolution
# ---------------------------------------------------------------------------
def resolve_token(symbol: str, chain_id: int = 8453) -> str | None:
    """Resolve a token symbol to its address on the given chain."""
    if chain_id == 8453:
        return BASE_TOKENS.get(symbol.upper())
    return None


# ---------------------------------------------------------------------------
# UniswapExecutor — Bus-integrated DEX execution
# ---------------------------------------------------------------------------
class UniswapExecutor:
    """Executes trades via Uniswap Trading API on Base.

    Listens for `exec.uniswap` events (published by the SOR when
    routing to the Uniswap venue) and performs the 3-step swap flow.

    For the ERC-8004 challenge, this provides DEX execution via
    a whitelisted router — trade intents signed with EIP-712 get
    settled on-chain through Uniswap's Universal Router.
    """

    def __init__(self, bus: Bus, private_key: str,
                 api_key: str | None = None,
                 chain_id: int = 8453):
        self.bus = bus
        self._api_key = api_key or os.getenv("UNISWAP_API_KEY", "")
        self._chain_id = chain_id

        # Wallet setup
        from eth_account import Account
        self._account = Account.from_key(private_key)
        self._address = self._account.address

        self._client = UniswapClient(self._api_key, chain_id) if self._api_key else None
        self._trade_count = 0
        self._total_volume_usd = 0.0

        bus.subscribe("exec.uniswap", self._on_exec)

    async def _on_exec(self, payload) -> None:
        """Handle Uniswap execution request."""
        intent: TradeIntent = payload if isinstance(payload, TradeIntent) else payload[0]

        if not self._client:
            log.warning("Uniswap API key not configured — cannot execute DEX trade")
            await self._report(intent, "rejected", reason="no_uniswap_api_key")
            return

        try:
            result = await self.execute_swap(intent)
            self._trade_count += 1
            self._total_volume_usd += intent.amount_in

            await self._report(
                intent, "filled",
                tx_hash=result.get("tx_hash", ""),
                fill_price=result.get("execution_price", 0),
                slippage=result.get("price_impact_bps", 0) / 10_000,
                reason=f"Uniswap {result.get('route_type', 'CLASSIC')} on Base",
            )

        except Exception as e:
            log.error("Uniswap execution failed for %s: %s", intent.id, e)
            await self._report(intent, "error", reason=f"uniswap: {e}")

    async def execute_swap(self, intent: TradeIntent) -> dict:
        """Execute a swap via the 3-step Trading API flow.

        Returns dict with tx_hash, execution_price, route_type, etc.
        """
        # Resolve tokens
        buying = intent.asset_in.upper() in QUOTE_ASSETS or intent.asset_in.upper() in _STABLES
        if buying:
            token_in_sym = intent.asset_in.upper()
            token_out_sym = intent.asset_out.upper()
        else:
            token_in_sym = intent.asset_in.upper()
            token_out_sym = intent.asset_out.upper()

        token_in = resolve_token(token_in_sym, self._chain_id)
        token_out = resolve_token(token_out_sym, self._chain_id)

        if not token_in or not token_out:
            raise ValueError(f"Cannot resolve tokens: {token_in_sym} -> {token_out_sym}")

        # Amount in wei/smallest unit (USDC = 6 decimals, ETH = 18)
        decimals = 6 if token_in_sym in _STABLES else 18
        amount_raw = str(int(intent.amount_in * (10 ** decimals)))

        log.info("Uniswap quote: %s %s -> %s (amount=%s, chain=%d)",
                 intent.id, token_in_sym, token_out_sym, amount_raw, self._chain_id)

        # Step 1: Check approval (skip for native ETH)
        if token_in != "0x0000000000000000000000000000000000000000":
            approval = await self._client.check_approval(
                token_in, self._address, amount_raw,
            )
            if approval.get("approval"):
                log.info("  Token approval needed — tx: %s", approval["approval"])
                # In production: sign and submit approval tx
                # For hackathon demo: log it
                await self.bus.publish("uniswap.approval_needed", {
                    "intent_id": intent.id,
                    "token": token_in_sym,
                    "approval_tx": approval["approval"],
                })

        # Step 2: Get quote
        quote = await self._client.get_quote(
            token_in=token_in,
            token_out=token_out,
            amount=amount_raw,
            swapper=self._address,
        )

        route_type = quote.get("routing", "CLASSIC")
        amount_out = quote.get("quote", {}).get("amount", "0")
        gas_estimate = quote.get("gasFee", "0")

        # Compute execution price
        out_decimals = 6 if token_out_sym in _STABLES else 18
        amount_out_float = int(amount_out) / (10 ** out_decimals) if amount_out != "0" else 0
        amount_in_float = intent.amount_in
        exec_price = amount_in_float / max(amount_out_float, 1e-18) if buying else amount_out_float / max(amount_in_float, 1e-18)

        log.info("  Quote: %s -> %s (route=%s, gas=%s)",
                 amount_raw, amount_out, route_type, gas_estimate)

        # Step 3: Get swap calldata
        swap = await self._client.get_swap(quote)
        swap_tx = swap.get("swap", {})
        tx_to = swap_tx.get("to", "")
        tx_data = swap_tx.get("data", "")
        tx_value = swap_tx.get("value", "0")

        # Sign and submit transaction
        # Use Flashbots Protect RPC when configured — sends tx to private
        # mempool, preventing sandwich attacks and other MEV extraction.
        from web3 import Web3
        default_rpc = "https://mainnet.base.org"
        flashbots_rpc = os.getenv("FLASHBOTS_RPC", "")
        rpc_url = flashbots_rpc or default_rpc
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        using_private_mempool = bool(flashbots_rpc)

        # Cap gas price to prevent runaway costs during spikes
        MAX_GAS_GWEI = int(os.getenv("MAX_GAS_PRICE_GWEI", "100"))
        max_gas_wei = MAX_GAS_GWEI * 10**9
        current_gas = w3.eth.gas_price
        if current_gas > max_gas_wei:
            raise ValueError(
                f"Gas price {current_gas / 10**9:.0f} gwei exceeds cap {MAX_GAS_GWEI} gwei — trade aborted"
            )

        nonce = w3.eth.get_transaction_count(self._address)
        tx = {
            "to": Web3.to_checksum_address(tx_to),
            "data": tx_data,
            "value": int(tx_value),
            "nonce": nonce,
            "gas": 300_000,
            "maxFeePerGas": min(current_gas * 2, max_gas_wei),
            "maxPriorityFeePerGas": min(w3.eth.max_priority_fee, max_gas_wei // 10),
            "chainId": self._chain_id,
            "type": 2,  # EIP-1559
        }

        signed = self._account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()

        protection = "flashbots_protect" if using_private_mempool else "none"
        log.info("  Swap tx submitted: %s (mev_protection=%s)", tx_hash_hex, protection)

        # Publish signed intent event for ERC-8004 tracking
        await self.bus.publish("uniswap.swap_submitted", {
            "intent_id": intent.id,
            "tx_hash": tx_hash_hex,
            "route_type": route_type,
            "token_in": token_in_sym,
            "token_out": token_out_sym,
            "amount_in": str(intent.amount_in),
            "amount_out": amount_out,
            "chain_id": self._chain_id,
            "mev_protection": protection,
        })

        return {
            "tx_hash": tx_hash_hex,
            "execution_price": exec_price,
            "route_type": route_type,
            "amount_out": amount_out,
            "gas_estimate": gas_estimate,
            "price_impact_bps": float(quote.get("priceImpact", 0)) * 10_000,
        }

    async def _report(self, intent: TradeIntent, status: str, *,
                      tx_hash: str = "", fill_price: float = 0,
                      slippage: float = 0, reason: str = "") -> None:
        """Publish execution report."""
        report = ExecutionReport(
            intent_id=intent.id,
            status=status,
            fill_price=fill_price,
            realized_slippage=slippage,
            pnl_estimate=0.0,
            notes=reason,
        )
        await self.bus.publish("exec.report", report)

    def status(self) -> dict:
        """Return Uniswap executor status."""
        return {
            "enabled": bool(self._client),
            "chain_id": self._chain_id,
            "address": self._address if hasattr(self, "_address") else None,
            "trade_count": self._trade_count,
            "total_volume_usd": round(self._total_volume_usd, 2),
            "api_configured": bool(self._api_key),
        }


# ---------------------------------------------------------------------------
# SOR venue integration — add Uniswap as a real DEX venue
# ---------------------------------------------------------------------------
def uniswap_venue_config() -> dict:
    """Return VenueConfig-compatible dict for adding Uniswap to the SOR."""
    return {
        "name": "uniswap_base",
        "base_url": TRADING_API_BASE,
        "fee_rate": 0.003,  # 0.3% typical Uniswap pool fee
        "enabled": bool(os.getenv("UNISWAP_API_KEY")),
        "weight": 0.95,     # high priority — real DEX execution
    }
