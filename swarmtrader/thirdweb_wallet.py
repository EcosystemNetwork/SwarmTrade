"""Thirdweb Wallet Provider — centralized wallet management via Thirdweb.

Replaces scattered eth_account / web3 usage with a single provider that
routes all chain interactions through Thirdweb's infrastructure:

  - RPC:     https://{chainId}.rpc.thirdweb.com/{clientId}
  - Signing: Local private key via eth_account (unchanged security model)
  - Tx send: Via Thirdweb-proxied RPC for reliability + analytics

Thirdweb benefits over raw RPC:
  - Automatic failover across RPC providers
  - Built-in request caching and rate-limit handling
  - Multi-chain support with a single client ID
  - Usage analytics on the Thirdweb dashboard

Environment variables:
  THIRDWEB_CLIENT_ID     — Thirdweb project client ID
  THIRDWEB_SECRET_KEY    — Thirdweb project secret key (server-side only)
  PRIVATE_KEY            — EVM private key for signing (unchanged)
"""
from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger("swarm.thirdweb")


def thirdweb_rpc(chain_id: int, client_id: str = "") -> str:
    """Build a Thirdweb RPC URL for the given chain.

    Falls back to public RPCs if no client ID is configured.
    """
    cid = client_id or os.getenv("THIRDWEB_CLIENT_ID", "")
    if cid:
        return f"https://{chain_id}.rpc.thirdweb.com/{cid}"
    # Fallback: public RPCs
    _FALLBACK = {
        1: "https://eth.llamarpc.com",
        8453: "https://mainnet.base.org",
        42161: "https://arb1.arbitrum.io/rpc",
        10: "https://mainnet.optimism.io",
        11155111: "https://rpc.sepolia.org",
    }
    return _FALLBACK.get(chain_id, f"https://{chain_id}.rpc.thirdweb.com")


class ThirdwebWallet:
    """Server-side wallet backed by Thirdweb RPC infrastructure.

    Wraps eth_account for local signing and routes all chain calls
    through Thirdweb's proxied RPC for reliability.

    Usage:
        wallet = ThirdwebWallet(private_key="0x...", chain_id=8453)
        signed = wallet.sign_transaction(tx_data)
        tx_hash = wallet.send_transaction(tx_data)
        # Or get the raw web3 instance:
        w3 = wallet.w3
    """

    def __init__(
        self,
        private_key: str = "",
        chain_id: int = 8453,
        client_id: str = "",
        secret_key: str = "",
    ):
        self._private_key = private_key or os.getenv("PRIVATE_KEY", "")
        self.chain_id = chain_id
        self.client_id = client_id or os.getenv("THIRDWEB_CLIENT_ID", "")
        self.secret_key = secret_key or os.getenv("THIRDWEB_SECRET_KEY", "")

        # Build RPC URL
        self.rpc_url = thirdweb_rpc(chain_id, self.client_id)

        # Initialize web3 with Thirdweb RPC
        self._w3 = None
        self._account = None
        self._address = ""

        if self._private_key:
            try:
                from eth_account import Account
                self._account = Account.from_key(self._private_key)
                self._address = self._account.address
            except ImportError:
                log.warning("eth_account not installed — wallet signing disabled")

        log.info("ThirdwebWallet: chain=%d rpc=%s address=%s",
                 chain_id, self.rpc_url[:60], self._address or "(no key)")

    @property
    def w3(self):
        """Lazy-initialized Web3 instance using Thirdweb RPC."""
        if self._w3 is None:
            from web3 import Web3
            # Attach secret key as bearer token for server-side auth
            provider_kwargs: dict[str, Any] = {}
            if self.secret_key:
                provider_kwargs["request_kwargs"] = {
                    "headers": {"x-secret-key": self.secret_key}
                }
            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url, **provider_kwargs))
        return self._w3

    @property
    def address(self) -> str:
        return self._address

    @property
    def account(self):
        """Raw eth_account Account object for direct signing."""
        return self._account

    @property
    def connected(self) -> bool:
        return bool(self._account)

    def sign_transaction(self, tx_data: dict) -> Any:
        """Sign a transaction dict. Returns SignedTransaction."""
        if not self._account:
            raise RuntimeError("No private key configured")
        return self._account.sign_transaction(tx_data)

    def sign_message(self, message) -> Any:
        """Sign an EIP-191 message. Accepts encode_defunct output."""
        if not self._account:
            raise RuntimeError("No private key configured")
        return self._account.sign_message(message)

    def sign_typed_data(self, domain: dict, types: dict, data: dict) -> Any:
        """Sign EIP-712 typed data."""
        if not self._account:
            raise RuntimeError("No private key configured")
        from eth_account.messages import encode_typed_data
        signable = encode_typed_data(domain, types, data)
        return self._account.sign_message(signable)

    def send_raw_transaction(self, signed_tx_bytes: bytes) -> str:
        """Submit a signed transaction via Thirdweb RPC. Returns tx hash hex."""
        tx_hash = self.w3.eth.send_raw_transaction(signed_tx_bytes)
        return tx_hash.hex()

    def send_transaction(self, tx_data: dict) -> str:
        """Sign and send a transaction in one call. Returns tx hash hex."""
        signed = self.sign_transaction(tx_data)
        return self.send_raw_transaction(signed.raw_transaction)

    def get_nonce(self) -> int:
        """Get current transaction count for this wallet."""
        return self.w3.eth.get_transaction_count(self._address)

    def get_gas_price(self) -> int:
        """Get current gas price from Thirdweb RPC."""
        return self.w3.eth.gas_price

    def get_balance(self) -> int:
        """Get native token balance in wei."""
        return self.w3.eth.get_balance(self._address)

    def for_chain(self, chain_id: int) -> "ThirdwebWallet":
        """Create a new wallet instance targeting a different chain.

        Reuses the same private key and Thirdweb credentials.
        """
        return ThirdwebWallet(
            private_key=self._private_key,
            chain_id=chain_id,
            client_id=self.client_id,
            secret_key=self.secret_key,
        )

    def contract(self, address: str, abi: list) -> Any:
        """Get a web3 Contract instance via Thirdweb RPC."""
        from web3 import Web3
        return self.w3.eth.contract(
            address=Web3.to_checksum_address(address),
            abi=abi,
        )

    def build_and_sign(self, contract_fn, *, gas: int = 200_000,
                       value: int = 0) -> tuple[str, bytes]:
        """Build a contract function call, sign it, return (tx_hash_hex, raw).

        Handles nonce, gas price, chain ID automatically.
        """
        nonce = self.get_nonce()
        tx = contract_fn.build_transaction({
            "from": self._address,
            "nonce": nonce,
            "gas": gas,
            "gasPrice": self.get_gas_price(),
            "chainId": self.chain_id,
            "value": value,
        })
        signed = self.sign_transaction(tx)
        tx_hash = self.send_raw_transaction(signed.raw_transaction)
        return tx_hash, signed.raw_transaction

    def status(self) -> dict:
        """Return wallet provider status."""
        return {
            "provider": "thirdweb",
            "chain_id": self.chain_id,
            "rpc_url": self.rpc_url,
            "address": self._address,
            "connected": self.connected,
            "client_id_set": bool(self.client_id),
            "secret_key_set": bool(self.secret_key),
        }
