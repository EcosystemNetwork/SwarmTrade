"""Stellar Multi-DEX Aggregator — StellarX, Lumenswap, Soroswap + SDEX.

Routes trades across all available Stellar DEX venues for best execution:

  1. SDEX (native)   — Stellar's built-in orderbook (manage_sell_offer)
  2. StellarX        — Aggregated SDEX + AMM liquidity via their API
  3. Lumenswap       — Stellar native AMM liquidity pools (Protocol 18)
  4. Soroswap        — Soroban-based Uniswap v2-style AMM

Architecture follows the same quoter pattern as dex_multi.py:
  - Each venue is a Quoter class with get_quote() -> StellarDEXQuote
  - StellarDEXAggregator runs all quoters in parallel, picks best price
  - Execution routes to the winning venue

Bus topics published:
  stellar.dex_quote      — quote from an individual venue
  stellar.best_route     — best route found across all venues
  stellar.arb            — cross-venue arbitrage opportunity

Environment variables:
  STELLAR_SECRET_KEY     — Stellar secret key for execution
  STELLAR_NETWORK        — "testnet" or "public"
  SOROSWAP_ROUTER        — Soroswap Router contract ID (auto-set per network)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
import aiohttp

from .core import Bus
from .stellar_payments import STELLAR_NETWORKS

log = logging.getLogger("swarm.stellar_dex_agg")

# ---------------------------------------------------------------------------
# Stellar asset identifiers
# ---------------------------------------------------------------------------

# Well-known Stellar assets (code:issuer, or "native" for XLM)
STELLAR_ASSETS = {
    "XLM": "native",
    "USDC": {
        "testnet": "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5",
        "public": "GA5ZSEJYB37JRC5AVCIA5MOP4RHTM335X2KGX3IHOJAPP5RE34K4KZVN",
    },
    "yUSDC": {
        "testnet": "GBBD47IF6LWK7P7MDEVSCWR7DPUWV3NY3DTQEVFL4NAT4AQH3ZLLFLA5",
        "public": "GDGTVWSM4MGS4T7Z6W4RPWOCHE2I6RDFCIFZGS3DOA63LWQTRNZNTTFF",
    },
    "BTC": {
        "public": "GDPJALI4AZKUU2W426U5WKMAT6CN3AJRPIIRYR2YM54TL2GDWO5O2MZM",
    },
    "ETH": {
        "public": "GBFXOHVAS43OIWNIO7XLRJAHT3BICFEYKOJW4MVOFQVERS2LUFC2LKPA",
    },
}


def resolve_issuer(code: str, network: str) -> str | None:
    """Resolve the correct issuer for a Stellar asset on a given network.

    Returns None for XLM (native), the issuer public key for known assets,
    or falls back to the network's USDC issuer for unknown codes.
    """
    upper = code.upper()
    if upper == "XLM":
        return None  # native asset

    asset_info = STELLAR_ASSETS.get(upper)
    if isinstance(asset_info, dict):
        issuer = asset_info.get(network)
        if issuer:
            return issuer
        # Asset exists but not on this network
        return None
    if isinstance(asset_info, str) and asset_info != "native":
        return asset_info

    # Fallback: assume USDC issuer (for custom assets on that issuer)
    net_cfg = STELLAR_NETWORKS.get(network, STELLAR_NETWORKS["testnet"])
    return net_cfg["usdc_issuer"]


def asset_type_for_code(code: str) -> str:
    """Return the Horizon asset_type string for a given code."""
    if code.upper() == "XLM":
        return "native"
    return "credit_alphanum4" if len(code) <= 4 else "credit_alphanum12"

# Soroswap Router contract IDs
SOROSWAP_CONTRACTS = {
    "testnet": {
        "router": "CDKDFC5BNKKQHM3SBREQWOIH7FMGWSRLKFRDB25RAZSYXQHVOZJJSR7Y",
        "factory": "CDLZFC3SYJYDZT7K67VZ75HPJVIEUVNIXF47ZG2FB2RMQQVU2HHGCYSC",
    },
    "public": {
        "router": "CARAMELH5P7SHTZ5BODQA3ZMSRKBNG7Z5OVTPBSLHKJIIDBMAWLONOAS",
        "factory": "CARAMELH5P7SHTZ5BODQA3ZMSRKBNG7Z5OVTPBSLHKJIIDBMAWLONOAS",
    },
}

# Soroswap SDK API (aggregator backend)
SOROSWAP_API = {
    "testnet": "https://api.soroswap.finance",
    "public": "https://api.soroswap.finance",
}


@dataclass
class StellarDEXQuote:
    """Standardized quote from any Stellar DEX venue."""
    source: str           # "sdex", "stellarx", "lumenswap", "soroswap"
    asset: str            # "XLM", "USDC", etc.
    side: str             # "buy" or "sell"
    price: float          # effective price per unit in counter asset
    amount_in: float      # input amount
    amount_out: float     # expected output amount
    fee_rate: float       # venue fee as decimal
    spread_bps: float     # bid-ask spread in basis points
    liquidity_usd: float  # estimated liquidity
    path: list[str] = field(default_factory=list)  # hop path
    pool_id: str = ""     # pool/offer ID
    network: str = "testnet"
    ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# SDEX Quoter — Stellar's native orderbook (already integrated, enhanced)
# ---------------------------------------------------------------------------

class SDEXQuoter:
    """Quote from the native Stellar Decentralized Exchange orderbook.

    Uses Horizon API directly. This is the existing SDEX integration
    wrapped as a quoter for the aggregator.
    """

    name = "sdex"

    def __init__(self, network: str = "testnet"):
        self._network = network
        net_cfg = STELLAR_NETWORKS.get(network, STELLAR_NETWORKS["testnet"])
        self._horizon = os.getenv("STELLAR_HORIZON_URL", net_cfg["horizon"])

    async def get_quote(
        self, session: aiohttp.ClientSession,
        selling: str, buying: str,
        amount: float = 1000.0,
    ) -> StellarDEXQuote | None:
        """Fetch best price from the SDEX orderbook via Horizon."""
        try:
            sell_params = self._asset_params("selling", selling)
            buy_params = self._asset_params("buying", buying)
            params = {**sell_params, **buy_params, "limit": "10"}

            url = f"{self._horizon}/order_book"
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            bids = data.get("bids", [])
            asks = data.get("asks", [])
            if not bids or not asks:
                return None

            best_bid = float(bids[0]["price"])
            best_ask = float(asks[0]["price"])
            mid = (best_bid + best_ask) / 2
            spread_bps = ((best_ask - best_bid) / mid) * 10_000 if mid else 0

            # Volume-weighted price from top 5 levels
            bid_depth = sum(float(b["amount"]) * float(b["price"]) for b in bids[:5])
            ask_depth = sum(float(a["amount"]) * float(a["price"]) for a in asks[:5])

            return StellarDEXQuote(
                source=self.name,
                asset=selling,
                side="sell",
                price=mid,
                amount_in=amount,
                amount_out=amount / mid if mid else 0,
                fee_rate=0.0,  # SDEX has no protocol fee (only network fee ~0.00001 XLM)
                spread_bps=round(spread_bps, 2),
                liquidity_usd=bid_depth + ask_depth,
                path=[selling, buying],
                network=self._network,
            )

        except Exception as e:
            log.debug("SDEX quote failed %s/%s: %s", selling, buying, e)
            return None

    def _asset_params(self, prefix: str, code: str) -> dict:
        if code.upper() == "XLM":
            return {f"{prefix}_asset_type": "native"}
        issuer = resolve_issuer(code, self._network)
        if not issuer:
            return {}  # asset not available on this network
        return {
            f"{prefix}_asset_type": asset_type_for_code(code),
            f"{prefix}_asset_code": code,
            f"{prefix}_asset_issuer": issuer,
        }


# ---------------------------------------------------------------------------
# StellarX Quoter — aggregated SDEX + AMM via StellarX API
# ---------------------------------------------------------------------------

class StellarXQuoter:
    """Quotes from StellarX — the largest Stellar DEX frontend/aggregator.

    StellarX aggregates SDEX orderbook liquidity and Stellar AMM pools,
    providing a unified best-price view. Their API also surfaces market
    data not directly available from Horizon.

    API: https://api.stellarx.com (public, no auth, mainnet only)
    """

    name = "stellarx"
    API_BASE = "https://api.stellarx.com"

    def __init__(self, network: str = "testnet"):
        self._network = network
        if network != "public":
            log.debug("StellarX API is mainnet-only — quoter disabled on %s", network)

    async def get_quote(
        self, session: aiohttp.ClientSession,
        selling: str, buying: str,
        amount: float = 1000.0,
    ) -> StellarDEXQuote | None:
        """Fetch aggregated quote from StellarX.

        StellarX combines SDEX orderbook + Stellar AMM pool liquidity,
        returning the best available execution path.
        Only available on public (mainnet) network.
        """
        if self._network != "public":
            return None

        try:
            sell_issuer = resolve_issuer(selling, self._network)
            buy_issuer = resolve_issuer(buying, self._network)

            sell_id = "native" if selling.upper() == "XLM" else f"{selling}-{sell_issuer}"
            buy_id = "native" if buying.upper() == "XLM" else f"{buying}-{buy_issuer}"

            if (selling.upper() != "XLM" and not sell_issuer) or \
               (buying.upper() != "XLM" and not buy_issuer):
                return None

            # Market ticker endpoint
            url = f"{self.API_BASE}/markets/{sell_id}/{buy_id}/ticker"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            bid = float(data.get("bid", 0))
            ask = float(data.get("ask", 0))
            volume = float(data.get("volume", 0))

            if not bid or not ask:
                return None

            mid = (bid + ask) / 2
            spread_bps = ((ask - bid) / mid) * 10_000 if mid else 0

            return StellarDEXQuote(
                source=self.name,
                asset=selling,
                side="sell",
                price=mid,
                amount_in=amount,
                amount_out=amount / mid if mid else 0,
                fee_rate=0.001,  # StellarX doesn't add fees but AMM pools charge ~0.1%
                spread_bps=round(spread_bps, 2),
                liquidity_usd=volume,
                path=[selling, buying],
                network=self._network,
            )

        except Exception as e:
            log.debug("StellarX quote failed %s/%s: %s", selling, buying, e)
            return None

    async def get_markets(self, session: aiohttp.ClientSession) -> list[dict]:
        """Fetch all active markets from StellarX."""
        if self._network != "public":
            return []
        try:
            url = f"{self.API_BASE}/markets"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                return await resp.json()
        except Exception as e:
            log.debug("StellarX markets fetch failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# Lumenswap Quoter — Stellar native AMM liquidity pools
# ---------------------------------------------------------------------------

class LumenswapQuoter:
    """Quotes from Stellar's native AMM liquidity pools (Protocol 18).

    Lumenswap operates on Stellar's native AMM infrastructure (constant
    product pools introduced in Protocol 18). These pools run alongside
    the SDEX orderbook and are accessed via Horizon's liquidity_pools
    endpoint.

    The native AMM uses the standard x*y=k formula with a 0.3% fee
    (30 bps), similar to Uniswap v2.

    Horizon API: /liquidity_pools — lists all pools with reserves
    Path payments automatically route through AMM pools for best price.
    """

    name = "lumenswap"

    def __init__(self, network: str = "testnet"):
        self._network = network
        net_cfg = STELLAR_NETWORKS.get(network, STELLAR_NETWORKS["testnet"])
        self._horizon = os.getenv("STELLAR_HORIZON_URL", net_cfg["horizon"])

    def _build_asset_params(self, prefix: str, code: str) -> dict | None:
        """Build Horizon query params for an asset. Returns None if unavailable."""
        if code.upper() == "XLM":
            return {f"{prefix}_asset_type": "native"}
        issuer = resolve_issuer(code, self._network)
        if not issuer:
            return None
        return {
            f"{prefix}_asset_type": asset_type_for_code(code),
            f"{prefix}_asset_code": code,
            f"{prefix}_asset_issuer": issuer,
        }

    async def get_quote(
        self, session: aiohttp.ClientSession,
        selling: str, buying: str,
        amount: float = 1000.0,
    ) -> StellarDEXQuote | None:
        """Fetch quote from Stellar native AMM pools via Horizon.

        Uses the strict_receive_paths endpoint which automatically
        considers both SDEX orderbook and AMM pool liquidity,
        returning the best available path.
        """
        try:
            src_params = self._build_asset_params("source", selling)
            dst_params = self._build_asset_params("destination", buying)
            if not src_params or not dst_params:
                return None

            params = {
                **src_params, **dst_params,
                "destination_amount": f"{amount:.7f}",
            }

            url = f"{self._horizon}/paths/strict-receive"
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            records = data.get("_embedded", {}).get("records", [])
            if not records:
                return None

            # Best path = lowest source_amount for desired destination_amount
            best = min(records, key=lambda r: float(r.get("source_amount", "999999")))
            src_amount = float(best.get("source_amount", "0"))
            dst_amount = float(best.get("destination_amount", "0"))

            if src_amount <= 0 or dst_amount <= 0:
                return None

            price = src_amount / dst_amount
            path_hops = [p.get("asset_code", "XLM") for p in best.get("path", [])]

            # Estimate liquidity from alternate paths — if multiple paths exist,
            # sum the source amounts across all routes as a depth proxy
            liquidity_est = sum(
                float(r.get("source_amount", "0")) for r in records
            )

            return StellarDEXQuote(
                source=self.name,
                asset=selling,
                side="sell",
                price=price,
                amount_in=src_amount,
                amount_out=dst_amount,
                fee_rate=0.003,  # Stellar native AMM charges 0.3%
                spread_bps=0,    # path payments don't expose bid/ask spread
                liquidity_usd=round(liquidity_est, 2),
                path=[selling] + path_hops + [buying],
                network=self._network,
            )

        except Exception as e:
            log.debug("Lumenswap quote failed %s/%s: %s", selling, buying, e)
            return None

    async def get_pools(self, session: aiohttp.ClientSession,
                        asset_code: str = "USDC") -> list[dict]:
        """List Stellar native AMM liquidity pools for an asset."""
        try:
            if asset_code.upper() == "XLM":
                reserves_param = "native"
            else:
                issuer = resolve_issuer(asset_code, self._network)
                if not issuer:
                    return []
                reserves_param = f"{asset_code}:{issuer}"

            url = f"{self._horizon}/liquidity_pools"
            async with session.get(url, params={"reserves": reserves_param},
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()

            pools = []
            for rec in data.get("_embedded", {}).get("records", []):
                reserves = rec.get("reserves", [])
                pools.append({
                    "pool_id": rec.get("id", ""),
                    "fee_bps": rec.get("fee_bp", 30),
                    "total_shares": rec.get("total_shares", "0"),
                    "reserves": [
                        {
                            "asset": r.get("asset", "native"),
                            "amount": r.get("amount", "0"),
                        }
                        for r in reserves
                    ],
                })
            return pools

        except Exception as e:
            log.debug("Lumenswap pool query failed: %s", e)
            return []


# ---------------------------------------------------------------------------
# Soroswap Quoter — Soroban AMM (Uniswap v2 on Stellar)
# ---------------------------------------------------------------------------

class SoroswapQuoter:
    """Quotes from Soroswap — Uniswap v2-style AMM on Soroban.

    Soroswap is the first Soroban-based AMM on Stellar, using
    constant-product pools deployed as Soroban smart contracts.
    It provides deeper liquidity for long-tail assets not well
    served by the SDEX orderbook.

    The Soroswap SDK API provides off-chain route optimization
    that accounts for all registered pools.

    API: https://api.soroswap.finance (route optimization)
    Contracts: Router + Factory on Soroban
    """

    name = "soroswap"

    def __init__(self, network: str = "testnet"):
        self._network = network
        self._api_base = SOROSWAP_API.get(network, SOROSWAP_API["testnet"])
        self._contracts = SOROSWAP_CONTRACTS.get(network, SOROSWAP_CONTRACTS["testnet"])

    async def get_quote(
        self, session: aiohttp.ClientSession,
        selling: str, buying: str,
        amount: float = 1000.0,
    ) -> StellarDEXQuote | None:
        """Fetch optimized swap route from Soroswap API.

        The Soroswap API computes the optimal path across all
        registered Soroban liquidity pools.
        """
        try:
            # Soroswap uses Soroban contract addresses for tokens
            token_in = self._resolve_token(selling)
            token_out = self._resolve_token(buying)

            if not token_in or not token_out:
                return None

            # Convert amount to integer representation (7 decimals for Stellar)
            amount_raw = str(int(amount * 10_000_000))

            payload = {
                "amount": amount_raw,
                "tokenIn": token_in,
                "tokenOut": token_out,
                "tradeType": "EXACT_INPUT",
            }

            url = f"{self._api_base}/api/router"
            async with session.post(url, json=payload,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    # Fallback: try GET with query params
                    return await self._quote_fallback(session, selling, buying, amount)
                data = await resp.json()

            amount_out_raw = data.get("amountOut", data.get("amount_out", "0"))
            amount_out = int(amount_out_raw) / 10_000_000 if amount_out_raw else 0
            route = data.get("path", data.get("route", []))

            if amount_out <= 0:
                return None

            price = amount / amount_out

            return StellarDEXQuote(
                source=self.name,
                asset=selling,
                side="sell",
                price=price,
                amount_in=amount,
                amount_out=amount_out,
                fee_rate=0.003,  # Soroswap default pool fee: 0.3%
                spread_bps=0,
                liquidity_usd=0,
                path=route if route else [selling, buying],
                pool_id=self._contracts.get("router", ""),
                network=self._network,
            )

        except Exception as e:
            log.debug("Soroswap quote failed %s/%s: %s", selling, buying, e)
            return None

    async def _quote_fallback(
        self, session: aiohttp.ClientSession,
        selling: str, buying: str, amount: float,
    ) -> StellarDEXQuote | None:
        """Fallback: query Soroswap via GET endpoint or simulate."""
        try:
            token_in = self._resolve_token(selling)
            token_out = self._resolve_token(buying)
            amount_raw = str(int(amount * 10_000_000))

            url = f"{self._api_base}/api/router/quote"
            params = {
                "tokenIn": token_in,
                "tokenOut": token_out,
                "amount": amount_raw,
            }
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()

            amount_out = int(data.get("amountOut", "0")) / 10_000_000
            if amount_out <= 0:
                return None

            return StellarDEXQuote(
                source=self.name,
                asset=selling,
                side="sell",
                price=amount / amount_out,
                amount_in=amount,
                amount_out=amount_out,
                fee_rate=0.003,
                spread_bps=0,
                liquidity_usd=0,
                path=[selling, buying],
                pool_id=self._contracts.get("router", ""),
                network=self._network,
            )

        except Exception as e:
            log.debug("Soroswap fallback quote failed: %s", e)
            return None

    def _resolve_token(self, code: str) -> str:
        """Resolve asset code to Soroban contract address.

        Soroswap uses Stellar Asset Contract (SAC) wrappers for
        native Stellar assets, or direct Soroban token contracts.
        """
        if code.upper() == "XLM":
            return "native"
        issuer = resolve_issuer(code, self._network)
        if not issuer:
            return ""
        return f"{code}:{issuer}"

    @property
    def router_contract(self) -> str:
        return self._contracts.get("router", "")


# ---------------------------------------------------------------------------
# StellarDEXAggregator — best-price routing across all venues
# ---------------------------------------------------------------------------

class StellarDEXAggregator:
    """Aggregates quotes from all Stellar DEX venues and routes to best price.

    Runs as a service alongside StellarDEXAgent, querying all venues
    in parallel and publishing the best route to the bus.

    Supports:
      - Best-price discovery across SDEX + StellarX + Lumenswap + Soroswap
      - Cross-venue arbitrage detection
      - Automatic execution via the winning venue
    """

    name = "stellar_dex_agg"

    def __init__(
        self,
        bus: Bus,
        network: str = "testnet",
        interval: float = 15.0,
        assets: list[str] | None = None,
    ):
        self.bus = bus
        self._network = network
        self._interval = interval
        self._assets = assets or ["XLM"]
        self._stop = False

        # Initialize all quoters
        self.quoters = [
            SDEXQuoter(network),
            StellarXQuoter(network),
            LumenswapQuoter(network),
            SoroswapQuoter(network),
        ]

        # Stats
        self._scan_count = 0
        self._arb_count = 0
        self._quotes: dict[str, list[StellarDEXQuote]] = {}
        self._best_routes: dict[str, StellarDEXQuote] = {}
        self._errors = 0

        bus.subscribe("stellar.route_request", self._on_route_request)

    async def run(self):
        """Main loop: scan all venues periodically."""
        log.info("StellarDEXAggregator started: venues=%d assets=%s interval=%.0fs",
                 len(self.quoters), self._assets, self._interval)

        while not self._stop:
            try:
                await self._scan()
                self._scan_count += 1
            except Exception as e:
                self._errors += 1
                log.warning("Stellar DEX scan error: %s", e)

            await asyncio.sleep(self._interval)

    async def _scan(self):
        """Query all venues for all assets and find best routes."""
        async with aiohttp.ClientSession() as session:
            for asset in self._assets:
                quotes = await self._get_all_quotes(session, asset, "USDC")
                if not quotes:
                    continue

                self._quotes[asset] = quotes

                # Publish individual quotes
                for q in quotes:
                    await self.bus.publish("stellar.dex_quote", {
                        "source": q.source,
                        "asset": q.asset,
                        "price": q.price,
                        "amount_out": q.amount_out,
                        "fee_rate": q.fee_rate,
                        "spread_bps": q.spread_bps,
                        "path": q.path,
                        "network": q.network,
                        "ts": q.ts,
                    })

                # Find best route (lowest effective price including fees)
                best = min(quotes, key=lambda q: q.price * (1 + q.fee_rate))
                self._best_routes[asset] = best

                await self.bus.publish("stellar.best_route", {
                    "asset": asset,
                    "venue": best.source,
                    "price": best.price,
                    "amount_out": best.amount_out,
                    "fee_rate": best.fee_rate,
                    "path": best.path,
                    "all_venues": [
                        {"source": q.source, "price": q.price, "fee": q.fee_rate}
                        for q in quotes
                    ],
                })

                # Check for cross-venue arbitrage
                await self._check_arb(asset, quotes)

    async def _get_all_quotes(
        self, session: aiohttp.ClientSession,
        selling: str, buying: str,
        amount: float = 1000.0,
    ) -> list[StellarDEXQuote]:
        """Query all venues in parallel. Returns list of successful quotes."""
        tasks = [
            quoter.get_quote(session, selling, buying, amount)
            for quoter in self.quoters
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        quotes = []
        for r in results:
            if isinstance(r, StellarDEXQuote) and r.price > 0:
                quotes.append(r)
            elif isinstance(r, Exception):
                log.debug("Quoter error: %s", r)

        return quotes

    async def _check_arb(self, asset: str, quotes: list[StellarDEXQuote]):
        """Detect cross-venue arbitrage opportunities."""
        if len(quotes) < 2:
            return

        effective = [(q, q.price * (1 + q.fee_rate)) for q in quotes]
        cheapest = min(effective, key=lambda x: x[1])
        priciest = max(effective, key=lambda x: x[1])

        spread_bps = ((priciest[1] - cheapest[1]) / cheapest[1]) * 10_000

        # 15+ bps net spread = actionable arb
        if spread_bps >= 15:
            self._arb_count += 1
            await self.bus.publish("stellar.arb", {
                "asset": asset,
                "buy_venue": cheapest[0].source,
                "buy_price": cheapest[0].price,
                "sell_venue": priciest[0].source,
                "sell_price": priciest[0].price,
                "spread_bps": round(spread_bps, 2),
                "net_profit_est": round(
                    (priciest[1] - cheapest[1]) * cheapest[0].amount_out, 4
                ),
                "ts": time.time(),
            })
            log.info("STELLAR ARB: %s buy@%s(%.6f) sell@%s(%.6f) spread=%.1fbps",
                     asset, cheapest[0].source, cheapest[0].price,
                     priciest[0].source, priciest[0].price, spread_bps)

    async def _on_route_request(self, msg: dict):
        """Handle a route request from another component."""
        asset = msg.get("asset", "XLM")
        amount = msg.get("amount", 1000.0)
        buying = msg.get("buying", "USDC")

        async with aiohttp.ClientSession() as session:
            quotes = await self._get_all_quotes(session, asset, buying, amount)

        if quotes:
            best = min(quotes, key=lambda q: q.price * (1 + q.fee_rate))
            await self.bus.publish("stellar.route_result", {
                "asset": asset,
                "buying": buying,
                "amount": amount,
                "venue": best.source,
                "price": best.price,
                "amount_out": best.amount_out,
                "all_venues": [
                    {"source": q.source, "price": q.price, "amount_out": q.amount_out}
                    for q in quotes
                ],
            })

    async def execute_best_route(
        self,
        asset: str,
        side: str,
        amount: float,
        max_slippage_bps: float = 50,
    ) -> dict:
        """Execute a trade via the best available venue.

        Routes to the venue with the best effective price,
        then calls the appropriate execution method.
        """
        async with aiohttp.ClientSession() as session:
            buying = "USDC" if side == "sell" else asset
            selling = asset if side == "sell" else "USDC"
            quotes = await self._get_all_quotes(session, selling, buying, amount)

        if not quotes:
            return {"status": "no_quotes", "error": "no venues returned quotes"}

        best = min(quotes, key=lambda q: q.price * (1 + q.fee_rate))

        # Publish the execution intent
        await self.bus.publish("stellar.execute_trade", {
            "asset": asset,
            "side": side,
            "amount": amount,
            "price": best.price,
            "venue": best.source,
            "max_slippage_bps": max_slippage_bps,
        })

        return {
            "status": "routed",
            "venue": best.source,
            "price": best.price,
            "amount_out": best.amount_out,
            "fee_rate": best.fee_rate,
            "path": best.path,
        }

    def stop(self):
        self._stop = True

    def status(self) -> dict:
        return {
            "agent": self.name,
            "network": self._network,
            "venues": [q.name for q in self.quoters],
            "assets": self._assets,
            "scans": self._scan_count,
            "arbs_found": self._arb_count,
            "errors": self._errors,
            "best_routes": {
                k: {
                    "venue": v.source,
                    "price": round(v.price, 8),
                    "fee": v.fee_rate,
                }
                for k, v in self._best_routes.items()
            },
            "latest_quotes": {
                k: [
                    {"venue": q.source, "price": round(q.price, 8)}
                    for q in v
                ]
                for k, v in self._quotes.items()
            },
        }
