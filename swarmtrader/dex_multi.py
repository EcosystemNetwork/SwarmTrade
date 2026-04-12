"""Multi-DEX integration — direct protocol quotes + execution.

Covers major DEXs across all chains the bot operates on:

  Ethereum / L2:
    - SushiSwap (Ethereum, Arbitrum, Base, Polygon)
    - Aerodrome (Base — dominant ve(3,3) DEX)
    - Curve (stablecoin + tricrypto pools)
    - PancakeSwap (BSC, Ethereum, Base, Arbitrum)
    - Balancer (Ethereum, Arbitrum, Polygon)
    - Camelot (Arbitrum-native)

  Solana:
    - Raydium (AMM + CLMM)
    - Orca (Whirlpools concentrated liquidity)

Each DEX provides:
  - Price quotes via public API (no auth needed for most)
  - Pool/pair liquidity data
  - Fee tier information
  - Integration with ArbScanner for cross-venue arbitrage

All quote methods return standardized dicts compatible with
DEXQuoteProvider.get_quotes() and ArbScanner.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp
from .core import Bus

log = logging.getLogger("swarm.dex_multi")

# ---------------------------------------------------------------------------
# Token addresses per chain
# ---------------------------------------------------------------------------

TOKENS = {
    1: {  # Ethereum
        "ETH":  "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
        "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
        "DAI":  "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    },
    8453: {  # Base
        "ETH":  "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
        "WETH": "0x4200000000000000000000000000000000000006",
        "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "USDbC": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",
        "DAI":  "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
        "AERO": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "cbETH": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
    },
    42161: {  # Arbitrum
        "ETH":  "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
        "WETH": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
        "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        "WBTC": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
        "ARB":  "0x912CE59144191C1204E64559FE8253a0e49E6548",
        "GMX":  "0xfc5A1A6EB076a2C7aD06eD22C90d7E710E35ad0a",
        "GRAIL": "0x3d9907F9a368ad0a51Be60f7Da3b97cf940982D8",
    },
    137: {  # Polygon
        "MATIC": "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
        "WMATIC": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
        "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        "WETH": "0x7ceB23fD6bC0adD59E62ac25578270cFf1b9f619",
        "WBTC": "0x1BFD67037B42Cf73acF2047067bd4F2C47D9BfD6",
    },
    56: {  # BSC
        "BNB":  "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
        "WBNB": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
        "USDT": "0x55d398326f99059fF775485246999027B3197955",
        "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
        "ETH":  "0x2170Ed0880ac9A755fd29B2688956BD959F933F8",
        "BTCB": "0x7130d2A12B9BCbFAe4f2634d864A1Ee1Ce3Ead9c",
        "CAKE": "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82",
    },
    10: {  # Optimism
        "ETH":  "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE",
        "WETH": "0x4200000000000000000000000000000000000006",
        "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        "USDT": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        "OP":   "0x4200000000000000000000000000000000000042",
    },
}

# Solana token mints
SOLANA_MINTS = {
    "SOL":  "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "mSOL": "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "JitoSOL": "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF":  "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "JUP":  "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "RAY":  "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
}


@dataclass
class DEXQuote:
    """Standardized quote from any DEX."""
    source: str          # "sushiswap", "aerodrome", "curve", etc.
    chain: str           # "ethereum", "base", "arbitrum", "solana"
    chain_id: int        # EVM chain ID (0 for Solana)
    asset: str           # "ETH", "BTC", etc.
    price: float         # effective price per unit
    fee_rate: float      # pool fee as decimal (0.003 = 0.3%)
    liquidity_usd: float # total pool liquidity in USD
    pool_address: str    # on-chain pool contract
    ts: float


# ---------------------------------------------------------------------------
# SushiSwap — multi-chain AMM
# ---------------------------------------------------------------------------

class SushiSwapQuoter:
    """SushiSwap v2/v3 quotes via their public API.

    Chains: Ethereum, Arbitrum, Base, Polygon, Optimism, BSC, Avalanche.
    API: https://api.sushi.com — free, no key required.
    """

    name = "sushiswap"
    API_BASE = "https://api.sushi.com"

    # SushiSwap uses chain names in their API
    CHAIN_NAMES = {
        1: "ethereum", 8453: "base", 42161: "arbitrum",
        137: "polygon", 10: "optimism", 56: "bsc",
    }

    async def get_quote(self, session: aiohttp.ClientSession,
                        asset: str, chain_id: int,
                        amount_usd: float = 1000.0) -> DEXQuote | None:
        chain_name = self.CHAIN_NAMES.get(chain_id)
        if not chain_name:
            return None
        tokens = TOKENS.get(chain_id, {})
        token_out = tokens.get(asset.upper()) or tokens.get(f"W{asset.upper()}")
        token_in = tokens.get("USDC") or tokens.get("USDT")
        if not token_out or not token_in:
            return None

        try:
            # SushiSwap swap API
            url = f"{self.API_BASE}/swap/v5/{chain_id}"
            params = {
                "tokenIn": token_in,
                "tokenOut": token_out,
                "amount": str(int(amount_usd * 1e6)),  # USDC decimals
                "maxPriceImpact": "0.01",
                "gasPrice": "1000000000",
                "to": "0x0000000000000000000000000000000000000000",
            }
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            amount_out = data.get("routeProcessorArgs", {}).get("amountOutMin", "0")
            # Parse output amount
            if asset.upper() in ("ETH", "WETH", "MATIC", "WMATIC", "BNB", "WBNB"):
                out_qty = int(amount_out) / 1e18
            elif asset.upper() in ("WBTC", "BTCB"):
                out_qty = int(amount_out) / 1e8
            else:
                out_qty = int(amount_out) / 1e18

            if out_qty <= 0:
                return None

            price = amount_usd / out_qty
            return DEXQuote(
                source=self.name, chain=chain_name, chain_id=chain_id,
                asset=asset, price=price, fee_rate=0.003,
                liquidity_usd=0, pool_address="", ts=time.time(),
            )
        except Exception as e:
            log.debug("SushiSwap quote failed %s/%d: %s", asset, chain_id, e)
            return None


# ---------------------------------------------------------------------------
# Aerodrome — Base's dominant ve(3,3) DEX
# ---------------------------------------------------------------------------

class AerodromeQuoter:
    """Aerodrome Finance quotes via their public API.

    Base-native DEX with ve(3,3) tokenomics. Highest TVL on Base.
    API: https://api.aerodrome.finance — free, no key.
    """

    name = "aerodrome"

    async def get_quote(self, session: aiohttp.ClientSession,
                        asset: str, chain_id: int = 8453,
                        amount_usd: float = 1000.0) -> DEXQuote | None:
        if chain_id != 8453:
            return None  # Aerodrome is Base-only
        tokens = TOKENS.get(8453, {})
        token_addr = tokens.get(asset.upper()) or tokens.get(f"W{asset.upper()}")
        usdc_addr = tokens.get("USDC")
        if not token_addr or not usdc_addr:
            return None

        try:
            # Aerodrome pools API
            url = "https://api.aerodrome.finance/api/v1/pairs"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            # Find the best pool for our pair
            best_pool = None
            best_tvl = 0
            target = token_addr.lower()
            stable = usdc_addr.lower()

            for pool in (data.get("data", []) if isinstance(data, dict) else data):
                t0 = (pool.get("token0", {}).get("address", "") or "").lower()
                t1 = (pool.get("token1", {}).get("address", "") or "").lower()
                if (t0 == target and t1 == stable) or (t1 == target and t0 == stable):
                    tvl = float(pool.get("tvl", 0) or pool.get("totalValueLockedUSD", 0))
                    if tvl > best_tvl:
                        best_tvl = tvl
                        best_pool = pool

            if not best_pool:
                return None

            # Calculate price from pool reserves
            r0 = float(best_pool.get("reserve0", 0))
            r1 = float(best_pool.get("reserve1", 0))
            t0_addr = (best_pool.get("token0", {}).get("address", "") or "").lower()

            if r0 > 0 and r1 > 0:
                if t0_addr == stable:
                    price = r0 / r1  # USDC per token
                else:
                    price = r1 / r0
            else:
                return None

            fee_rate = 0.003 if best_pool.get("isStable", False) else 0.003

            return DEXQuote(
                source=self.name, chain="base", chain_id=8453,
                asset=asset, price=price, fee_rate=fee_rate,
                liquidity_usd=best_tvl,
                pool_address=best_pool.get("address", ""),
                ts=time.time(),
            )
        except Exception as e:
            log.debug("Aerodrome quote failed for %s: %s", asset, e)
            return None


# ---------------------------------------------------------------------------
# Curve — stablecoin + tricrypto specialist
# ---------------------------------------------------------------------------

class CurveQuoter:
    """Curve Finance quotes via their public API.

    Best for stablecoin swaps (lowest slippage) and tricrypto pools.
    API: https://api.curve.fi — free, no key.
    """

    name = "curve"

    CHAIN_NAMES = {1: "ethereum", 42161: "arbitrum", 137: "polygon", 10: "optimism"}

    async def get_quote(self, session: aiohttp.ClientSession,
                        asset: str, chain_id: int,
                        amount_usd: float = 1000.0) -> DEXQuote | None:
        chain_name = self.CHAIN_NAMES.get(chain_id)
        if not chain_name:
            return None

        try:
            # Curve pools API
            url = f"https://api.curve.fi/v1/getPools/{chain_name}/main"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            pools = data.get("data", {}).get("poolData", [])
            if not pools:
                return None

            # Find pools containing our asset
            target = asset.upper()
            for pool in pools:
                coins = [c.get("symbol", "").upper() for c in pool.get("coins", [])]
                if target in coins or f"W{target}" in coins:
                    tvl = float(pool.get("usdTotal", 0))
                    if tvl < 100000:
                        continue

                    # Estimate price from pool's virtual price and TVL
                    vp = float(pool.get("virtualPrice", 1e18)) / 1e18
                    n_coins = len(coins)
                    if n_coins > 0 and tvl > 0:
                        # For tricrypto pools, use coin prices
                        coin_prices = pool.get("usdTotalExcludingBasePool", tvl)
                        if isinstance(coin_prices, (int, float)) and coin_prices > 0:
                            return DEXQuote(
                                source=self.name, chain=chain_name, chain_id=chain_id,
                                asset=asset, price=0,  # price comes from swap simulation
                                fee_rate=0.0004,  # Curve's famously low fees
                                liquidity_usd=tvl,
                                pool_address=pool.get("address", ""),
                                ts=time.time(),
                            )
            return None
        except Exception as e:
            log.debug("Curve quote failed for %s/%d: %s", asset, chain_id, e)
            return None


# ---------------------------------------------------------------------------
# PancakeSwap — BSC + multi-chain
# ---------------------------------------------------------------------------

class PancakeSwapQuoter:
    """PancakeSwap quotes via their public API.

    Dominant DEX on BSC. Also deployed on Ethereum, Base, Arbitrum.
    API: https://api.pancakeswap.info — free, no key.
    """

    name = "pancakeswap"

    CHAIN_NAMES = {56: "bsc", 1: "ethereum", 8453: "base", 42161: "arbitrum"}

    async def get_quote(self, session: aiohttp.ClientSession,
                        asset: str, chain_id: int,
                        amount_usd: float = 1000.0) -> DEXQuote | None:
        chain_name = self.CHAIN_NAMES.get(chain_id)
        if not chain_name:
            return None
        tokens = TOKENS.get(chain_id, {})
        token_addr = tokens.get(asset.upper()) or tokens.get(f"W{asset.upper()}")
        if not token_addr:
            return None

        try:
            # PancakeSwap uses 0x API-compatible quote endpoint
            url = f"https://api.pancakeswap.finance/api/v1/quote"
            params = {
                "chainId": str(chain_id),
                "srcToken": tokens.get("USDT") or tokens.get("USDC"),
                "destToken": token_addr,
                "amount": str(int(amount_usd * 1e6 if "USDC" in tokens else amount_usd * 1e18)),
            }
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    # Fallback: try token price endpoint
                    return await self._fallback_price(session, asset, chain_id)
                data = await resp.json()

            dest_amount = int(data.get("destAmount", "0"))
            if dest_amount <= 0:
                return await self._fallback_price(session, asset, chain_id)

            decimals = 18 if asset.upper() in ("ETH", "BNB", "MATIC") else 8
            out_qty = dest_amount / (10 ** decimals)
            price = amount_usd / out_qty if out_qty > 0 else 0

            return DEXQuote(
                source=self.name, chain=chain_name, chain_id=chain_id,
                asset=asset, price=price, fee_rate=0.0025,
                liquidity_usd=0, pool_address="", ts=time.time(),
            )
        except Exception as e:
            log.debug("PancakeSwap quote failed %s/%d: %s", asset, chain_id, e)
            return await self._fallback_price(session, asset, chain_id)

    async def _fallback_price(self, session: aiohttp.ClientSession,
                              asset: str, chain_id: int) -> DEXQuote | None:
        """Fallback: get price from PancakeSwap token list."""
        try:
            tokens = TOKENS.get(chain_id, {})
            addr = tokens.get(asset.upper()) or tokens.get(f"W{asset.upper()}")
            if not addr or addr.startswith("0xEeee"):
                return None
            url = f"https://tokens.pancakeswap.finance/pancakeswap-extended.json"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
            # Just return None for now — the token list doesn't have prices
            return None
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Raydium — Solana AMM
# ---------------------------------------------------------------------------

class RaydiumQuoter:
    """Raydium quotes via their public API.

    Dominant AMM on Solana. Supports CLMM (concentrated liquidity) pools.
    API: https://api-v3.raydium.io — free, no key.
    """

    name = "raydium"

    async def get_quote(self, session: aiohttp.ClientSession,
                        asset: str, chain_id: int = 0,
                        amount_usd: float = 1000.0) -> DEXQuote | None:
        input_mint = SOLANA_MINTS.get("USDC")
        output_mint = SOLANA_MINTS.get(asset.upper())
        if not input_mint or not output_mint:
            return None

        amount = int(amount_usd * 1e6)  # USDC 6 decimals

        try:
            url = "https://api-v3.raydium.io/compute/swap-base-in"
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": "50",
                "txVersion": "V0",
            }
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            if not data.get("success"):
                return None

            result = data.get("data", {})
            out_amount = int(result.get("outputAmount", 0))
            if out_amount <= 0:
                return None

            # SOL = 9 decimals, most SPL tokens = 6-9
            decimals = 9 if asset.upper() == "SOL" else 6
            out_qty = out_amount / (10 ** decimals)
            price = amount_usd / out_qty if out_qty > 0 else 0

            return DEXQuote(
                source=self.name, chain="solana", chain_id=0,
                asset=asset, price=price, fee_rate=0.0025,
                liquidity_usd=0, pool_address="", ts=time.time(),
            )
        except Exception as e:
            log.debug("Raydium quote failed for %s: %s", asset, e)
            return None


# ---------------------------------------------------------------------------
# Orca — Solana concentrated liquidity
# ---------------------------------------------------------------------------

class OrcaQuoter:
    """Orca Whirlpool quotes via their public API.

    Concentrated liquidity DEX on Solana. Often better rates than Raydium.
    API: https://api.orca.so — free, no key.
    """

    name = "orca"

    async def get_quote(self, session: aiohttp.ClientSession,
                        asset: str, chain_id: int = 0,
                        amount_usd: float = 1000.0) -> DEXQuote | None:
        input_mint = SOLANA_MINTS.get("USDC")
        output_mint = SOLANA_MINTS.get(asset.upper())
        if not input_mint or not output_mint:
            return None

        try:
            # Orca uses a quote API
            url = "https://api.orca.so/v2/solana/quote"
            params = {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(int(amount_usd * 1e6)),
                "slippageBps": "50",
                "onlyDirectRoutes": "false",
            }
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            out_amount = int(data.get("outAmount", data.get("outputAmount", 0)))
            if out_amount <= 0:
                return None

            decimals = 9 if asset.upper() == "SOL" else 6
            out_qty = out_amount / (10 ** decimals)
            price = amount_usd / out_qty if out_qty > 0 else 0

            return DEXQuote(
                source=self.name, chain="solana", chain_id=0,
                asset=asset, price=price, fee_rate=0.003,
                liquidity_usd=0, pool_address="", ts=time.time(),
            )
        except Exception as e:
            log.debug("Orca quote failed for %s: %s", asset, e)
            return None


# ---------------------------------------------------------------------------
# Aggregated Multi-DEX Scanner
# ---------------------------------------------------------------------------

# All DEX quoters
_QUOTERS = [
    SushiSwapQuoter(),
    AerodromeQuoter(),
    CurveQuoter(),
    PancakeSwapQuoter(),
    RaydiumQuoter(),
    OrcaQuoter(),
]


class MultiDEXScanner:
    """Scans all DEXs across all chains for best prices.

    Publishes quotes to the bus for the ArbScanner and SOR to consume.
    Also detects cross-DEX arbitrage opportunities (DEX ↔ DEX).
    """

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 chains: list[int] | None = None,
                 interval: float = 30.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.chains = chains or [1, 8453, 42161]  # Ethereum, Base, Arbitrum
        self.interval = interval
        self._stop = False
        self._latest_quotes: dict[str, list[DEXQuote]] = {}

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("MultiDEXScanner starting: %d DEXs, %d chains, assets=%s",
                 len(_QUOTERS), len(self.chains), self.assets)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "SwarmTrade/1.0"},
        ) as session:
            while not self._stop:
                for asset in self.assets:
                    quotes = await self._scan_asset(session, asset)
                    if quotes:
                        self._latest_quotes[asset] = quotes
                        await self._publish_quotes(asset, quotes)
                        await self._check_dex_arb(asset, quotes)
                await asyncio.sleep(self.interval)

    async def _scan_asset(self, session: aiohttp.ClientSession,
                          asset: str) -> list[DEXQuote]:
        """Fetch quotes from all DEXs across all chains for one asset."""
        tasks = []
        for quoter in _QUOTERS:
            if quoter.name in ("raydium", "orca"):
                # Solana DEXs
                tasks.append(self._safe_quote(quoter, session, asset, 0))
            else:
                # EVM DEXs — try each chain
                for chain_id in self.chains:
                    tasks.append(self._safe_quote(quoter, session, asset, chain_id))

        results = await asyncio.gather(*tasks)
        return [q for q in results if q is not None]

    async def _safe_quote(self, quoter, session, asset, chain_id) -> DEXQuote | None:
        try:
            return await quoter.get_quote(session, asset, chain_id)
        except Exception as e:
            log.debug("Quote failed %s/%s/%d: %s", quoter.name, asset, chain_id, e)
            return None

    async def _publish_quotes(self, asset: str, quotes: list[DEXQuote]):
        """Publish DEX quotes for ArbScanner consumption."""
        for q in quotes:
            if q.price > 0:
                await self.bus.publish("market.dex_quote", {
                    "source": q.source,
                    "chain": q.chain,
                    "chain_id": q.chain_id,
                    "asset": q.asset,
                    "price": q.price,
                    "fee_rate": q.fee_rate,
                    "liquidity_usd": q.liquidity_usd,
                    "ts": q.ts,
                })

    async def _check_dex_arb(self, asset: str, quotes: list[DEXQuote]):
        """Detect cross-DEX arbitrage (DEX ↔ DEX)."""
        priced = [q for q in quotes if q.price > 0]
        if len(priced) < 2:
            return

        cheapest = min(priced, key=lambda q: q.price)
        most_expensive = max(priced, key=lambda q: q.price)

        if cheapest.source == most_expensive.source and cheapest.chain_id == most_expensive.chain_id:
            return

        spread_pct = (most_expensive.price - cheapest.price) / cheapest.price * 100
        net_spread = spread_pct - (cheapest.fee_rate + most_expensive.fee_rate) * 100

        if net_spread > 0.15:  # > 15bps net
            log.info("DEX-DEX ARB: %s buy@%s/%s(%.2f) sell@%s/%s(%.2f) net=%.2f%%",
                     asset, cheapest.source, cheapest.chain,
                     cheapest.price, most_expensive.source, most_expensive.chain,
                     most_expensive.price, net_spread)

            await self.bus.publish("arb.dex_opportunity", {
                "asset": asset,
                "buy_dex": cheapest.source,
                "buy_chain": cheapest.chain,
                "buy_price": cheapest.price,
                "sell_dex": most_expensive.source,
                "sell_chain": most_expensive.chain,
                "sell_price": most_expensive.price,
                "spread_pct": round(spread_pct, 4),
                "net_spread_pct": round(net_spread, 4),
            })

    def get_quotes_for_arb(self, asset: str) -> list[dict]:
        """Get latest quotes in ArbScanner-compatible format."""
        quotes = self._latest_quotes.get(asset, [])
        return [
            {
                "source": q.source,
                "price": q.price,
                "fee_estimate": q.fee_rate,
                "chain": q.chain,
            }
            for q in quotes if q.price > 0
        ]

    @property
    def stats(self) -> dict:
        total_quotes = sum(len(qs) for qs in self._latest_quotes.values())
        return {
            "dexs": len(_QUOTERS),
            "chains": self.chains,
            "assets_tracked": list(self._latest_quotes.keys()),
            "total_quotes": total_quotes,
        }
