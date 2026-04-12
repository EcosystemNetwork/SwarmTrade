"""Polymarket Prediction Market Signals — event-driven trading intelligence.

Inspired by LeAgent (ETHGlobal Cannes 2026) which uses Polymarket data
to inform trading decisions. Monitors prediction markets for:
  - Crypto-relevant event probabilities (regulatory, ETF, fork, etc.)
  - Rapid probability shifts (signal something is happening)
  - Market sentiment from prediction market pricing

Data source: Polymarket CLOB API (free, no auth for read)

Publishes:
  signal.polymarket     — directional signal when events shift
  intelligence.polymarket — raw prediction market data

No environment variables required (public API).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .core import Bus, Signal

log = logging.getLogger("swarm.polymarket")

# Polymarket CLOB API
POLYMARKET_API = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Rate limiting
_last_api_call: dict[str, float] = {}
_MIN_API_INTERVAL = 1.0


async def _rate_limit(api_name: str):
    now = time.time()
    last = _last_api_call.get(api_name, 0.0)
    wait = _MIN_API_INTERVAL - (now - last)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_api_call[api_name] = time.time()


# Keywords that make a prediction market crypto-relevant
CRYPTO_KEYWORDS = {
    "bitcoin", "btc", "ethereum", "eth", "crypto", "cryptocurrency",
    "sec", "etf", "defi", "stablecoin", "usdc", "usdt", "tether",
    "coinbase", "binance", "solana", "sol", "cardano", "ripple", "xrp",
    "regulation", "fed", "interest rate", "inflation", "cpi",
    "trump", "election", "tariff", "sanctions",  # political events that move crypto
    "hack", "exploit", "rug pull",
}


@dataclass
class PredictionMarket:
    """A prediction market from Polymarket."""
    market_id: str
    question: str
    description: str
    outcome_yes_price: float  # 0-1 probability
    outcome_no_price: float
    volume_usd: float
    liquidity_usd: float
    end_date: str
    category: str
    crypto_relevance: float  # 0-1 how relevant to crypto
    tags: list[str] = field(default_factory=list)


@dataclass
class MarketShift:
    """A significant probability shift in a prediction market."""
    market_id: str
    question: str
    old_probability: float
    new_probability: float
    change: float  # absolute change
    timeframe_hours: float
    crypto_impact: str  # "bullish", "bearish", "neutral"
    confidence: float


class PolymarketAgent:
    """Monitors Polymarket for crypto-relevant prediction markets.

    Strategy:
      1. Fetch active markets from Polymarket API
      2. Filter for crypto-relevant events
      3. Track probability changes over time
      4. Generate signals when probabilities shift significantly
      5. Map prediction market movements to crypto directional signals

    Signal logic:
      - Pro-crypto regulation passing: bullish
      - SEC enforcement action likely: bearish
      - Rate cut probability rising: bullish
      - Geopolitical instability rising: mixed (short-term bearish, long-term bullish)
    """

    name = "polymarket"

    def __init__(
        self,
        bus: Bus,
        assets: list[str] | None = None,
        interval: float = 300.0,
        min_volume: float = 10_000.0,
        min_shift: float = 0.05,  # 5% probability shift to trigger signal
    ):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self.min_volume = min_volume
        self.min_shift = min_shift
        self._stop = False
        self._markets: dict[str, PredictionMarket] = {}
        self._price_history: dict[str, list[tuple[float, float]]] = {}  # market_id -> [(ts, price)]
        self._max_history = 100

    def stop(self):
        self._stop = True

    async def run(self):
        log.info(
            "PolymarketAgent starting: interval=%.0fs min_volume=$%.0f min_shift=%.0f%%",
            self.interval, self.min_volume, self.min_shift * 100,
        )
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                try:
                    await self._fetch_markets(session)
                    shifts = self._detect_shifts()
                    for shift in shifts:
                        await self._publish_signal(shift)
                    await self._publish_intelligence()
                except Exception as e:
                    log.warning("Polymarket error: %s", e)
                await asyncio.sleep(self.interval)

    async def _fetch_markets(self, session: aiohttp.ClientSession):
        """Fetch active prediction markets from Polymarket."""
        await _rate_limit("polymarket")

        try:
            # Fetch from Gamma API (market metadata)
            url = f"{GAMMA_API}/markets?closed=false&limit=100&order=volume24hr&ascending=false"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                if resp.status != 200:
                    log.debug("Gamma API error: %d", resp.status)
                    return
                markets_data = await resp.json()

            for m in markets_data:
                question = (m.get("question") or "").lower()
                description = (m.get("description") or "").lower()
                combined = f"{question} {description}"

                # Calculate crypto relevance
                relevance = self._calc_crypto_relevance(combined)
                if relevance < 0.3:
                    continue

                market_id = m.get("id", "")
                volume = float(m.get("volume", 0) or 0)
                if volume < self.min_volume:
                    continue

                # Get current prices from outcomes
                outcomes = m.get("outcomePrices", "")
                if isinstance(outcomes, str) and outcomes:
                    try:
                        import json
                        prices = json.loads(outcomes)
                        yes_price = float(prices[0]) if prices else 0.5
                        no_price = float(prices[1]) if len(prices) > 1 else 1 - yes_price
                    except (json.JSONDecodeError, IndexError, ValueError):
                        yes_price, no_price = 0.5, 0.5
                elif isinstance(outcomes, list) and outcomes:
                    yes_price = float(outcomes[0])
                    no_price = float(outcomes[1]) if len(outcomes) > 1 else 1 - yes_price
                else:
                    yes_price, no_price = 0.5, 0.5

                market = PredictionMarket(
                    market_id=market_id,
                    question=m.get("question", ""),
                    description=m.get("description", "")[:200],
                    outcome_yes_price=yes_price,
                    outcome_no_price=no_price,
                    volume_usd=volume,
                    liquidity_usd=float(m.get("liquidity", 0) or 0),
                    end_date=m.get("endDate", ""),
                    category=m.get("category", ""),
                    crypto_relevance=relevance,
                    tags=m.get("tags", []) or [],
                )

                # Track price history
                self._price_history.setdefault(market_id, []).append(
                    (time.time(), yes_price)
                )
                # Trim history
                if len(self._price_history[market_id]) > self._max_history:
                    self._price_history[market_id] = self._price_history[market_id][-self._max_history:]

                self._markets[market_id] = market

            log.info("Polymarket: tracking %d crypto-relevant markets", len(self._markets))

        except Exception as e:
            log.warning("Polymarket fetch error: %s", e)

    @staticmethod
    def _calc_crypto_relevance(text: str) -> float:
        """Calculate how relevant a market is to crypto trading."""
        text_lower = text.lower()
        matches = sum(1 for kw in CRYPTO_KEYWORDS if kw in text_lower)
        # Normalize: 1 match = 0.3, 2 = 0.6, 3+ = 0.8+
        if matches == 0:
            return 0.0
        return min(1.0, 0.3 + (matches - 1) * 0.2)

    def _detect_shifts(self) -> list[MarketShift]:
        """Detect significant probability shifts across tracked markets."""
        shifts: list[MarketShift] = []
        now = time.time()

        for market_id, market in self._markets.items():
            history = self._price_history.get(market_id, [])
            if len(history) < 2:
                continue

            # Compare current price to price from ~1 hour ago
            current_price = history[-1][1]
            old_price = current_price

            for ts, price in reversed(history):
                if now - ts >= 3600:  # 1 hour ago
                    old_price = price
                    break

            change = current_price - old_price

            if abs(change) < self.min_shift:
                continue

            # Determine crypto impact based on market content
            impact = self._assess_crypto_impact(market, change)

            shifts.append(MarketShift(
                market_id=market_id,
                question=market.question,
                old_probability=old_price,
                new_probability=current_price,
                change=change,
                timeframe_hours=1.0,
                crypto_impact=impact,
                confidence=min(0.7, market.crypto_relevance * abs(change) * 5),
            ))

        return shifts

    def _assess_crypto_impact(self, market: PredictionMarket, change: float) -> str:
        """Assess whether a probability shift is bullish or bearish for crypto."""
        q = market.question.lower()

        # Bullish events (probability going UP = bullish for crypto)
        bullish_keywords = ["etf", "approval", "rate cut", "adoption", "legal"]
        # Bearish events (probability going UP = bearish for crypto)
        bearish_keywords = ["ban", "enforcement", "tax", "restriction", "crash", "recession"]

        bullish_score = sum(1 for kw in bullish_keywords if kw in q)
        bearish_score = sum(1 for kw in bearish_keywords if kw in q)

        if bullish_score > bearish_score:
            return "bullish" if change > 0 else "bearish"
        elif bearish_score > bullish_score:
            return "bearish" if change > 0 else "bullish"
        return "neutral"

    async def _publish_signal(self, shift: MarketShift):
        """Convert a prediction market shift into a trading signal."""
        if shift.crypto_impact == "neutral":
            return

        direction = "long" if shift.crypto_impact == "bullish" else "short"
        strength = min(0.6, abs(shift.change) * 3)  # Cap at 0.6 — these are slow signals

        for asset in self.assets:
            sig = Signal(
                agent_id="polymarket",
                asset=asset,
                direction=direction,
                strength=strength,
                confidence=shift.confidence,
                rationale=(
                    f"Polymarket: '{shift.question[:80]}' "
                    f"{shift.old_probability:.0%}->{shift.new_probability:.0%} "
                    f"({shift.change:+.0%}) impact={shift.crypto_impact}"
                ),
            )
            await self.bus.publish("signal.polymarket", sig)

        log.info(
            "Polymarket signal: %s (%.0%% shift) '%s' -> %s",
            shift.crypto_impact, abs(shift.change) * 100,
            shift.question[:60], direction,
        )

    async def _publish_intelligence(self):
        """Publish raw prediction market intelligence."""
        if not self._markets:
            return

        # Sort by volume
        sorted_markets = sorted(
            self._markets.values(),
            key=lambda m: m.volume_usd,
            reverse=True,
        )[:20]

        intel = {
            "ts": time.time(),
            "tracked_markets": len(self._markets),
            "top_markets": [
                {
                    "question": m.question[:100],
                    "yes_probability": round(m.outcome_yes_price, 3),
                    "volume_usd": round(m.volume_usd, 0),
                    "liquidity_usd": round(m.liquidity_usd, 0),
                    "crypto_relevance": round(m.crypto_relevance, 2),
                    "category": m.category,
                }
                for m in sorted_markets
            ],
        }
        await self.bus.publish("intelligence.polymarket", intel)

    def summary(self) -> dict:
        return {
            "tracked_markets": len(self._markets),
            "top_market": (
                self._markets[max(self._markets, key=lambda k: self._markets[k].volume_usd)].question[:80]
                if self._markets else "none"
            ),
            "total_volume": round(
                sum(m.volume_usd for m in self._markets.values()), 0
            ),
        }
