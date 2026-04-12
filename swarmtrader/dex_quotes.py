"""DEX aggregation quotes via 1inch API — real multi-venue on-chain pricing.

Fetches real swap quotes from 1inch Fusion+ across multiple DEXs
to provide true best-execution pricing for the Smart Order Router.

Supported chains:
  1 = Ethereum, 8453 = Base, 137 = Polygon, 42161 = Arbitrum, 10 = Optimism
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from .core import Bus

log = logging.getLogger("swarm.dex")

_1INCH_API = "https://api.1inch.dev/swap/v6.0"

# Common token addresses per chain
_TOKENS: dict[int, dict[str, str]] = {
    1: {  # Ethereum mainnet
        "ETH": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
        "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    },
    8453: {  # Base
        "ETH": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
        "WETH": "0x4200000000000000000000000000000000000006",
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
    },
    42161: {  # Arbitrum
        "ETH": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
        "WETH": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    },
}


class DEXQuoteProvider:
    """Fetches real swap quotes from 1inch DEX aggregator.

    Provides actual on-chain pricing that the SOR can use for
    true multi-venue best execution (CEX vs DEX comparison).
    """

    def __init__(self, bus: Bus, chain_id: int = 1,
                 api_key: str = "", interval: float = 15.0):
        self.bus = bus
        self.chain_id = chain_id
        self.api_key = api_key
        self.interval = interval
        self._stop = False
        self._session: aiohttp.ClientSession | None = None
        self._quotes: dict[str, dict] = {}  # symbol -> latest quote

    def stop(self):
        self._stop = True

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"Accept": "application/json"}
            if self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
            self._session = aiohttp.ClientSession(headers=headers)
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_quote(self, from_token: str, to_token: str,
                        amount_usd: float = 1000.0) -> dict | None:
        """Get a swap quote from 1inch.

        Args:
            from_token: Symbol (e.g., "USDC")
            to_token: Symbol (e.g., "ETH")
            amount_usd: Amount in USD terms

        Returns dict with:
            from_token, to_token, from_amount, to_amount,
            effective_price, gas_estimate, protocols (DEXs used)
        """
        tokens = _TOKENS.get(self.chain_id, {})
        src_addr = tokens.get(from_token.upper())
        dst_addr = tokens.get(to_token.upper())
        if not src_addr or not dst_addr:
            log.debug("Unknown token: %s or %s on chain %d",
                      from_token, to_token, self.chain_id)
            return None

        # Convert USD amount to token decimals (USDC = 6 decimals)
        if from_token.upper() in ("USDC", "USDT"):
            amount_wei = int(amount_usd * 1e6)
        elif from_token.upper() in ("ETH", "WETH"):
            # Need price to convert — skip if we don't know it
            return None
        else:
            amount_wei = int(amount_usd * 1e18)

        session = await self._ensure_session()
        url = f"{_1INCH_API}/{self.chain_id}/quote"
        params = {
            "src": src_addr,
            "dst": dst_addr,
            "amount": str(amount_wei),
        }

        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.debug("1inch quote failed (%d): %s", resp.status, body[:200])
                    return None
                data = await resp.json()
        except Exception as e:
            log.debug("1inch quote error: %s", e)
            return None

        # Parse response
        dst_amount_raw = int(data.get("dstAmount", 0))
        if dst_amount_raw <= 0:
            return None

        # Convert destination amount based on token decimals
        if to_token.upper() in ("USDC", "USDT"):
            dst_amount = dst_amount_raw / 1e6
        elif to_token.upper() in ("ETH", "WETH"):
            dst_amount = dst_amount_raw / 1e18
        elif to_token.upper() == "WBTC":
            dst_amount = dst_amount_raw / 1e8
        else:
            dst_amount = dst_amount_raw / 1e18

        effective_price = amount_usd / dst_amount if dst_amount > 0 else 0

        quote = {
            "from_token": from_token,
            "to_token": to_token,
            "from_amount": amount_usd,
            "to_amount": dst_amount,
            "effective_price": effective_price,
            "gas_estimate": int(data.get("gas", 0)),
            "chain_id": self.chain_id,
            "source": "1inch",
            "ts": time.time(),
        }

        self._quotes[to_token.upper()] = quote
        return quote

    async def run(self):
        """Periodically fetch DEX quotes for comparison with CEX."""
        log.info("DEXQuoteProvider starting: chain=%d interval=%.0fs", self.chain_id, self.interval)

        while not self._stop:
            try:
                # Quote ETH and BTC from USDC
                for asset in ("ETH", "WBTC"):
                    quote = await self.get_quote("USDC", asset, 1000.0)
                    if quote:
                        await self.bus.publish("market.dex_quote", quote)
            except Exception as e:
                log.debug("DEX quote cycle error: %s", e)

            await asyncio.sleep(self.interval)

    @property
    def latest_quotes(self) -> dict[str, dict]:
        return dict(self._quotes)
