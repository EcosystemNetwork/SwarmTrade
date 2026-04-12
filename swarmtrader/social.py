"""Social sentiment agent: tracks Reddit and social media activity
for crypto assets to detect hype cycles and retail momentum.

Uses free public APIs:
  - Reddit (pushshift-style or old.reddit.com JSON endpoints)
  - CoinGecko community data (free, no key)

Social hype often precedes retail-driven pumps.
Declining social volume after a pump signals exhaustion.
"""
from __future__ import annotations
import asyncio, logging, os, time
from collections import deque
import aiohttp
from .core import Bus, Signal

log = logging.getLogger("swarm.social")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
REDDIT_SEARCH = "https://www.reddit.com/r/CryptoCurrency/search.json"

# CoinGecko ID mapping
ASSET_TO_COINGECKO = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "XRP": "ripple", "ADA": "cardano", "DOT": "polkadot",
    "LINK": "chainlink", "AVAX": "avalanche-2", "DOGE": "dogecoin",
    "MATIC": "matic-network", "LTC": "litecoin",
}


class SocialSentimentAgent:
    """Monitors social media activity and community metrics to detect
    hype cycles and sentiment shifts.

    Data sources:
      1. CoinGecko community data (Twitter followers, Reddit subscribers,
         developer activity)
      2. Reddit post volume and engagement for crypto subreddits

    Publishes signal.social per asset.
    """

    name = "social"

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 300.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False

        # Track community metrics over time
        self._community_history: dict[str, deque[dict]] = {
            a: deque(maxlen=24) for a in self.assets
        }
        self._reddit_history: dict[str, deque[int]] = {
            a: deque(maxlen=24) for a in self.assets
        }

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("SocialSentimentAgent starting: assets=%s", self.assets)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                for asset in self.assets:
                    await self._analyze_asset(session, asset)
                await asyncio.sleep(self.interval)

    async def _analyze_asset(self, session: aiohttp.ClientSession, asset: str):
        # Fetch both data sources in parallel
        cg_data, reddit_count = await asyncio.gather(
            self._fetch_coingecko(session, asset),
            self._fetch_reddit_volume(session, asset),
            return_exceptions=True,
        )

        if isinstance(cg_data, Exception):
            cg_data = {}
        if isinstance(reddit_count, Exception):
            reddit_count = 0

        strength = 0.0
        confidence = 0.0
        rationale_parts = []

        # --- CoinGecko community metrics ---
        if cg_data:
            community = cg_data.get("community_data", {})
            dev = cg_data.get("developer_data", {})
            sentiment_up = cg_data.get("sentiment_votes_up_percentage", 50)
            sentiment_down = cg_data.get("sentiment_votes_down_percentage", 50)

            # Community sentiment from CoinGecko votes
            if sentiment_up is not None and sentiment_down is not None:
                net_sentiment = (sentiment_up - sentiment_down) / 100  # -1 to 1
                strength += net_sentiment * 0.5
                rationale_parts.append(f"cg_sent={net_sentiment:+.2f}")

            # Reddit subscriber growth signal
            reddit_subs = community.get("reddit_subscribers", 0)
            reddit_active = community.get("reddit_accounts_active_48h", 0)
            if reddit_subs > 0 and reddit_active > 0:
                activity_ratio = reddit_active / reddit_subs
                # High activity ratio = unusual engagement = potential move
                if activity_ratio > 0.01:  # >1% of subs active
                    activity_signal = min(0.3, (activity_ratio - 0.005) * 30)
                    strength += activity_signal
                    rationale_parts.append(f"reddit_activity={activity_ratio:.4f}")

            # Developer activity as long-term bullish signal
            commits = dev.get("commit_count_4_weeks", 0) or 0
            if commits > 100:
                strength += 0.1  # active development = bullish
                rationale_parts.append(f"dev_commits={commits}")

            self._community_history[asset].append({
                "ts": time.time(),
                "sentiment_up": sentiment_up,
                "reddit_active": reddit_active,
            })

        # --- Reddit post volume ---
        if isinstance(reddit_count, int) and reddit_count > 0:
            self._reddit_history[asset].append(reddit_count)

            if len(self._reddit_history[asset]) >= 3:
                hist = list(self._reddit_history[asset])
                avg = sum(hist[:-1]) / len(hist[:-1])
                if avg > 0:
                    volume_ratio = reddit_count / avg
                    if volume_ratio > 2.0:
                        # Social volume spike = potential pump incoming
                        spike_signal = min(0.4, (volume_ratio - 1) * 0.2)
                        strength += spike_signal
                        rationale_parts.append(
                            f"reddit_spike={volume_ratio:.1f}x ({reddit_count}posts)")
                    elif volume_ratio < 0.5:
                        # Declining interest = bearish
                        strength -= 0.15
                        rationale_parts.append(f"reddit_quiet={volume_ratio:.2f}x")

        strength = max(-1.0, min(1.0, strength))
        if abs(strength) < 0.05:
            return

        # Confidence based on data availability
        data_sources = bool(cg_data) + bool(reddit_count)
        confidence = min(1.0, 0.3 + data_sources * 0.25)

        sig = Signal(
            self.name, asset,
            "long" if strength > 0 else "short",
            strength, confidence,
            " ".join(rationale_parts) or "social_neutral",
        )
        await self.bus.publish("signal.social", sig)

    async def _fetch_coingecko(self, session: aiohttp.ClientSession,
                                asset: str) -> dict:
        cg_id = ASSET_TO_COINGECKO.get(asset.upper())
        if not cg_id:
            return {}
        url = f"{COINGECKO_BASE}/coins/{cg_id}"
        params = {
            "localization": "false",
            "tickers": "false",
            "market_data": "false",
            "community_data": "true",
            "developer_data": "true",
            "sparkline": "false",
        }
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return {}
                return await resp.json()
        except Exception as e:
            log.debug("CoinGecko %s failed: %s", asset, e)
            return {}

    async def _fetch_reddit_volume(self, session: aiohttp.ClientSession,
                                    asset: str) -> int:
        """Count recent Reddit posts mentioning this asset."""
        headers = {"User-Agent": "SwarmTrader/1.0"}
        params = {
            "q": asset,
            "sort": "new",
            "limit": "25",
            "restrict_sr": "true",
            "t": "day",
        }
        try:
            async with session.get(REDDIT_SEARCH, params=params,
                                   headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return 0
                data = await resp.json()
                children = data.get("data", {}).get("children", [])
                return len(children)
        except Exception as e:
            log.debug("Reddit search %s failed: %s", asset, e)
            return 0
