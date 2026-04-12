"""BirdEye API Agent — Solana token analytics and market intelligence.

Inspired by Moon Dev's housecoin bots that use BirdEye for price data,
token discovery, and liquidity analysis on Solana.

Features:
  - Real-time token prices across all Solana DEXes
  - Token security/audit scores (rug pull detection)
  - Trending tokens and new listings discovery
  - Liquidity depth analysis
  - Wallet portfolio tracking
  - Historical price data for backtesting

Publishes:
  signal.birdeye            — trading signals from Solana analytics
  intelligence.birdeye      — raw analytics data
  intelligence.trending     — trending Solana tokens
  intelligence.token_security — rug pull risk scores

Environment variables:
  BIRDEYE_API_KEY           — BirdEye API key (get from birdeye.so)
  BIRDEYE_CHAIN             — Chain to monitor (default: solana)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

import aiohttp

from .core import Bus, Signal

log = logging.getLogger("swarm.birdeye")

BIRDEYE_BASE_URL = "https://public-api.birdeye.so"

# Known Solana token addresses
SOLANA_TOKENS: dict[str, str] = {
    "SOL": "So11111111111111111111111111111111111111112",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
    "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
    "PYTH": "HZ1JovNiVvGrGNiiYvEozEVgZ58xaU3RKwX8eACQBCt3",
    "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
    "ORCA": "orcaEKTdK7LKz57vaAYr9QeNsVEPfiu6QeMU1kektZE",
    "RENDER": "rndrizKT3MK1iimdxRdWabcF7Zg7AR5T4nud4EkHBof",
    "W": "85VBFQZC9TZkfaptBWjvUw7YbZjy52A6mjtPGjstQAmQ",
    "PEPE": "9MjAmgHXbu5drkNa9HpjGCbPfuFME22M4ZcEFkF3zy7D",
}


@dataclass
class TokenInfo:
    """Comprehensive token data from BirdEye."""
    address: str
    symbol: str
    name: str
    price: float
    price_change_24h: float
    volume_24h: float
    liquidity: float
    market_cap: float
    holders: int = 0
    # Security
    is_verified: bool = False
    has_freeze_authority: bool = False
    has_mint_authority: bool = False
    top10_holder_pct: float = 0.0
    # Analytics
    buy_count_24h: int = 0
    sell_count_24h: int = 0
    unique_wallets_24h: int = 0


@dataclass
class TrendingToken:
    """A trending token on Solana."""
    symbol: str
    address: str
    price: float
    volume_24h: float
    price_change_24h: float
    rank: int
    score: float  # trending score


# ---------------------------------------------------------------------------
# BirdEye Client
# ---------------------------------------------------------------------------
class BirdEyeClient:
    """Async client for BirdEye API."""

    def __init__(self, api_key: str | None = None, chain: str = "solana"):
        self.api_key = api_key or os.getenv("BIRDEYE_API_KEY", "")
        self.chain = chain
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            headers = {"X-API-KEY": self.api_key} if self.api_key else {}
            headers["x-chain"] = self.chain
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers=headers,
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _get(self, endpoint: str, params: dict | None = None) -> dict | None:
        if not self.api_key:
            return None
        session = await self._get_session()
        try:
            async with session.get(f"{BIRDEYE_BASE_URL}{endpoint}",
                                   params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.warning("BirdEye %s %d", endpoint, resp.status)
        except Exception as e:
            log.warning("BirdEye error %s: %s", endpoint, e)
        return None

    # -- Token Data --

    async def get_token_price(self, address: str) -> float | None:
        """Get current USD price for a token."""
        data = await self._get("/defi/price", {"address": address})
        if data and data.get("success"):
            return data.get("data", {}).get("value")
        return None

    async def get_multi_price(self, addresses: list[str]) -> dict[str, float]:
        """Get prices for multiple tokens."""
        prices = {}
        # BirdEye multi-price endpoint
        data = await self._get("/defi/multi_price",
                               {"list_address": ",".join(addresses)})
        if data and data.get("success"):
            for addr, info in data.get("data", {}).items():
                if info and info.get("value"):
                    symbol = SOLANA_TOKENS.get(addr, addr[:8])
                    # Reverse lookup
                    for sym, mint in SOLANA_TOKENS.items():
                        if mint == addr:
                            symbol = sym
                            break
                    prices[symbol] = float(info["value"])
        return prices

    async def get_token_overview(self, address: str) -> TokenInfo | None:
        """Get comprehensive token info including security data."""
        data = await self._get("/defi/token_overview", {"address": address})
        if not data or not data.get("success"):
            return None
        d = data.get("data", {})
        return TokenInfo(
            address=address,
            symbol=d.get("symbol", ""),
            name=d.get("name", ""),
            price=float(d.get("price", 0)),
            price_change_24h=float(d.get("priceChange24hPercent", 0)),
            volume_24h=float(d.get("v24hUSD", 0)),
            liquidity=float(d.get("liquidity", 0)),
            market_cap=float(d.get("mc", 0)),
            holders=int(d.get("holder", 0)),
            buy_count_24h=int(d.get("buy24h", 0)),
            sell_count_24h=int(d.get("sell24h", 0)),
            unique_wallets_24h=int(d.get("uniqueWallet24h", 0)),
        )

    async def get_token_security(self, address: str) -> dict:
        """Get token security/audit data (rug pull detection)."""
        data = await self._get("/defi/token_security", {"address": address})
        if data and data.get("success"):
            return data.get("data", {})
        return {}

    async def get_trending_tokens(self, limit: int = 20) -> list[TrendingToken]:
        """Get currently trending tokens."""
        data = await self._get("/defi/token_trending",
                               {"sort_by": "rank", "sort_type": "asc",
                                "offset": "0", "limit": str(limit)})
        if not data or not data.get("success"):
            return []
        tokens = []
        for i, item in enumerate(data.get("data", {}).get("items", [])):
            tokens.append(TrendingToken(
                symbol=item.get("symbol", ""),
                address=item.get("address", ""),
                price=float(item.get("price", 0)),
                volume_24h=float(item.get("v24hUSD", 0)),
                price_change_24h=float(item.get("priceChange24hPercent", 0)),
                rank=i + 1,
                score=float(item.get("rank", 0)),
            ))
        return tokens

    async def get_new_listings(self, limit: int = 20) -> list[dict]:
        """Get newly listed tokens."""
        data = await self._get("/defi/v2/tokens/new_listing",
                               {"limit": str(limit)})
        if data and data.get("success"):
            return data.get("data", {}).get("items", [])
        return []

    async def get_ohlcv(self, address: str, interval: str = "1H",
                        limit: int = 100) -> list[dict]:
        """Get OHLCV data for a token. interval: 1m, 5m, 15m, 1H, 4H, 1D."""
        now = int(time.time())
        # Estimate time range from interval
        interval_map = {"1m": 60, "5m": 300, "15m": 900,
                        "1H": 3600, "4H": 14400, "1D": 86400}
        seconds = interval_map.get(interval, 3600)
        time_from = now - (seconds * limit)

        data = await self._get("/defi/ohlcv", {
            "address": address,
            "type": interval,
            "time_from": str(time_from),
            "time_to": str(now),
        })
        if data and data.get("success"):
            return data.get("data", {}).get("items", [])
        return []

    async def get_wallet_portfolio(self, wallet: str) -> dict:
        """Get wallet holdings and portfolio value."""
        data = await self._get("/v1/wallet/token_list",
                               {"wallet": wallet})
        if data and data.get("success"):
            return data.get("data", {})
        return {}


# ---------------------------------------------------------------------------
# BirdEye Signal Agent
# ---------------------------------------------------------------------------
class BirdEyeAgent:
    """Generates trading signals from Solana token analytics.

    Monitors:
      - Volume spikes (sudden interest in a token)
      - Liquidity changes (rug pull warning)
      - Buy/sell ratio imbalance
      - Trending token momentum
      - New listing opportunities
      - Token security scores
    """

    def __init__(self, bus: Bus, tokens: list[str] | None = None,
                 interval: float = 30.0):
        self.bus = bus
        self.tokens = tokens or ["SOL", "JUP", "BONK", "WIF", "PYTH", "RAY"]
        self.interval = interval
        self.client = BirdEyeClient()
        self._running = False
        self._prev_volumes: dict[str, float] = {}
        self._prev_prices: dict[str, float] = {}
        self._token_info_cache: dict[str, TokenInfo] = {}

    async def run(self):
        self._running = True
        log.info("BirdEye agent started: %s (interval=%.0fs)", self.tokens, self.interval)

        while self._running:
            await self._tick()
            await asyncio.sleep(self.interval)

    async def stop(self):
        self._running = False
        await self.client.close()

    async def _tick(self):
        try:
            # Fetch token data + trending concurrently
            tasks = []
            for token in self.tokens:
                addr = SOLANA_TOKENS.get(token)
                if addr:
                    tasks.append(self._analyze_token(token, addr))
            tasks.append(self._check_trending())

            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            log.warning("BirdEye tick error: %s", e)

    async def _analyze_token(self, symbol: str, address: str):
        """Analyze a single token and generate signals."""
        info = await self.client.get_token_overview(address)
        if not info:
            return

        self._token_info_cache[symbol] = info
        signals: list[tuple[float, float, str]] = []

        # 1. Volume spike detection
        prev_vol = self._prev_volumes.get(symbol, 0)
        if prev_vol > 0 and info.volume_24h > prev_vol * 2:
            volume_mult = info.volume_24h / prev_vol
            signals.append((1.0 if info.price_change_24h > 0 else -1.0,
                           min(volume_mult / 5, 0.5),
                           f"volume spike {volume_mult:.1f}x"))
        self._prev_volumes[symbol] = info.volume_24h

        # 2. Buy/sell ratio
        total_trades = info.buy_count_24h + info.sell_count_24h
        if total_trades > 100:
            buy_ratio = info.buy_count_24h / total_trades
            if buy_ratio > 0.6:
                signals.append((1.0, (buy_ratio - 0.5) * 2,
                               f"buy ratio {buy_ratio:.1%}"))
            elif buy_ratio < 0.4:
                signals.append((-1.0, (0.5 - buy_ratio) * 2,
                               f"sell ratio {1-buy_ratio:.1%}"))

        # 3. Price momentum
        if abs(info.price_change_24h) > 5:
            direction = 1.0 if info.price_change_24h > 0 else -1.0
            signals.append((direction, min(abs(info.price_change_24h) / 20, 0.4),
                           f"24h change {info.price_change_24h:+.1f}%"))

        # 4. Liquidity check (low liquidity = danger)
        if info.liquidity < 50_000 and info.volume_24h > 100_000:
            signals.append((-1.0, 0.3, f"low liquidity ${info.liquidity:,.0f}"))

        # Publish intelligence
        await self.bus.publish("intelligence.birdeye", {
            "symbol": symbol,
            "price": info.price,
            "volume_24h": info.volume_24h,
            "price_change_24h": info.price_change_24h,
            "liquidity": info.liquidity,
            "holders": info.holders,
            "buy_count": info.buy_count_24h,
            "sell_count": info.sell_count_24h,
            "unique_wallets": info.unique_wallets_24h,
            "ts": time.time(),
        })

        # Check token security
        security = await self.client.get_token_security(address)
        if security:
            risk_score = self._compute_risk_score(security)
            await self.bus.publish("intelligence.token_security", {
                "symbol": symbol,
                "address": address,
                "risk_score": risk_score,
                "has_freeze": security.get("freezeAuthority") is not None,
                "has_mint": security.get("mintAuthority") is not None,
                "top10_pct": security.get("top10HolderPercent", 0),
                "ts": time.time(),
            })
            # High risk = don't trade
            if risk_score > 0.7:
                signals.append((-1.0, 0.8, f"HIGH RISK score={risk_score:.2f}"))

        # Aggregate and publish signal
        if signals:
            total_weight = sum(abs(w) for _, w, _ in signals)
            if total_weight > 0.1:
                weighted_dir = sum(d * abs(w) for d, w, _ in signals) / total_weight
                confidence = min(total_weight / 1.5, 0.9)
                reasons = "; ".join(r for _, _, r in signals)

                if abs(weighted_dir) > 0.15:
                    signal = Signal(
                        agent_id="birdeye",
                        asset=symbol,
                        direction="long" if weighted_dir > 0 else "short",
                        strength=min(abs(weighted_dir), 1.0),
                        confidence=confidence,
                        rationale=f"[BirdEye] {reasons}",
                    )
                    await self.bus.publish("signal.birdeye", signal)

    async def _check_trending(self):
        """Monitor trending tokens for opportunities."""
        trending = await self.client.get_trending_tokens(limit=10)
        if not trending:
            return

        await self.bus.publish("intelligence.trending", {
            "tokens": [
                {
                    "symbol": t.symbol,
                    "price": t.price,
                    "volume": t.volume_24h,
                    "change_24h": t.price_change_24h,
                    "rank": t.rank,
                }
                for t in trending
            ],
            "ts": time.time(),
        })

    def _compute_risk_score(self, security: dict) -> float:
        """Compute a 0-1 risk score from security data. Higher = riskier."""
        score = 0.0
        # Freeze authority = can freeze your tokens
        if security.get("freezeAuthority"):
            score += 0.3
        # Mint authority = can mint unlimited tokens
        if security.get("mintAuthority"):
            score += 0.3
        # Top 10 holders own >50%
        top10 = float(security.get("top10HolderPercent", 0))
        if top10 > 50:
            score += 0.2
        elif top10 > 30:
            score += 0.1
        # Not verified
        if not security.get("isVerified", False):
            score += 0.1
        return min(score, 1.0)

    def get_token_info(self, symbol: str) -> TokenInfo | None:
        """Get cached token info."""
        return self._token_info_cache.get(symbol)
