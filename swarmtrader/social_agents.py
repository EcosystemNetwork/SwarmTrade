"""Autonomous Social Media Agents — X/Discord/Telegram.

Inspired by Moon Dev's ZerePy (Twitter agent framework) and Eliza
(multi-agent with social connectors). Provides autonomous agents
that monitor and post to social media platforms for:
  - Real-time alpha from crypto Twitter/X
  - Trade signal sharing on Discord/Telegram
  - Sentiment harvesting from social feeds
  - Automated trade notifications

Agents:
  XMonitorAgent     — monitors X/Twitter for crypto alpha
  DiscordAgent      — posts trade signals to Discord webhooks
  TelegramAgent     — sends alerts via Telegram bot
  SocialAggregator  — combines all social signals into one feed

Publishes:
  signal.social_alpha       — alpha signals from X/Twitter
  intelligence.x_feed       — raw X/Twitter feed data
  intelligence.social_post  — outbound post confirmations
  notification.trade        — trade notifications to all channels

Environment variables:
  X_BEARER_TOKEN            — X/Twitter API v2 Bearer token
  X_API_KEY                 — X API key (for posting)
  X_API_SECRET              — X API secret
  X_ACCESS_TOKEN            — X access token (for posting)
  X_ACCESS_SECRET           — X access token secret
  DISCORD_WEBHOOK_URL       — Discord webhook for trade alerts
  DISCORD_ALPHA_WEBHOOK     — Discord webhook for alpha signals
  TELEGRAM_BOT_TOKEN        — Telegram bot token
  TELEGRAM_CHAT_ID          — Telegram chat/channel ID
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field

import aiohttp

from .core import Bus, Signal

log = logging.getLogger("swarm.social_agents")


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------
@dataclass
class SocialPost:
    """A social media post/tweet."""
    platform: str          # "x", "discord", "telegram"
    author: str
    text: str
    ts: float
    engagement: int = 0    # likes + retweets
    url: str = ""
    # Extracted signals
    tickers: list[str] = field(default_factory=list)
    sentiment: float = 0.0  # -1 to 1
    is_alpha: bool = False


@dataclass
class TradeNotification:
    """A trade notification to broadcast."""
    asset: str
    side: str
    price: float
    quantity: float
    pnl: float | None = None
    strategy: str = ""
    ts: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Ticker Extraction + Sentiment
# ---------------------------------------------------------------------------
# Common crypto tickers to watch for
CRYPTO_TICKERS = {
    "BTC", "ETH", "SOL", "AVAX", "ARB", "OP", "LINK", "DOT", "ADA",
    "XRP", "DOGE", "MATIC", "ATOM", "NEAR", "SUI", "APT", "SEI", "TIA",
    "INJ", "JUP", "PEPE", "WIF", "BONK", "PYTH", "RENDER", "FET",
    "ONDO", "JTO", "RAY", "ORCA", "HBAR", "FTM",
}

# Bullish/bearish keywords for simple sentiment
BULLISH_WORDS = {
    "bullish", "moon", "pump", "breakout", "accumulate", "buy", "long",
    "ath", "rally", "surge", "green", "rocket", "send it", "lfg",
    "undervalued", "dip buy", "bottom", "reversal", "golden cross",
}
BEARISH_WORDS = {
    "bearish", "dump", "crash", "sell", "short", "rug", "scam",
    "overvalued", "top", "resistance", "death cross", "red",
    "liquidated", "rekt", "capitulation", "fear",
}

# Known alpha accounts on X (curated list)
ALPHA_ACCOUNTS = {
    "cobie", "hsaka_", "lookonchain", "EmberCN", "ai_9684xtpa",
    "whale_alert", "DefiIgnas", "Pentosh1", "CryptoKaleo",
    "TheCryptoDog", "AltcoinGordon", "MoonOverlord",
}


def extract_tickers(text: str) -> list[str]:
    """Extract crypto tickers from text ($BTC, #ETH, etc)."""
    # Match $TICKER or #TICKER patterns
    matches = re.findall(r'[\$#]([A-Z]{2,10})\b', text.upper())
    # Also match standalone ticker mentions
    words = set(text.upper().split())
    found = []
    for m in matches:
        if m in CRYPTO_TICKERS:
            found.append(m)
    for w in words:
        clean = re.sub(r'[^A-Z]', '', w)
        if clean in CRYPTO_TICKERS and clean not in found:
            found.append(clean)
    return found


def simple_sentiment(text: str) -> float:
    """Compute simple sentiment score from -1 (bearish) to 1 (bullish)."""
    text_lower = text.lower()
    bull_count = sum(1 for w in BULLISH_WORDS if w in text_lower)
    bear_count = sum(1 for w in BEARISH_WORDS if w in text_lower)
    total = bull_count + bear_count
    if total == 0:
        return 0.0
    return (bull_count - bear_count) / total


# ---------------------------------------------------------------------------
# X/Twitter Monitor Agent
# ---------------------------------------------------------------------------
class XMonitorAgent:
    """Monitors X/Twitter for crypto alpha using the v2 API.

    Tracks:
      - Keyword searches for crypto tickers
      - Known alpha account tweets
      - Engagement-weighted sentiment
      - Real-time filtered stream (if elevated access)
    """

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 60.0):
        self.bus = bus
        self.assets = assets or ["BTC", "ETH", "SOL"]
        self.interval = interval
        self.bearer_token = os.getenv("X_BEARER_TOKEN", "")
        self._session: aiohttp.ClientSession | None = None
        self._running = False
        self._seen_ids: set[str] = set()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Authorization": f"Bearer {self.bearer_token}"},
            )
        return self._session

    async def run(self):
        if not self.bearer_token:
            log.info("X monitor disabled (no X_BEARER_TOKEN)")
            return

        self._running = True
        log.info("X monitor started: %s (interval=%.0fs)", self.assets, self.interval)

        while self._running:
            await self._search_crypto_tweets()
            await asyncio.sleep(self.interval)

    async def stop(self):
        self._running = False
        if self._session and not self._session.closed:
            await self._session.close()

    async def _search_crypto_tweets(self):
        """Search for recent crypto tweets with high engagement."""
        session = await self._get_session()

        # Build query: top crypto tickers + alpha accounts
        tickers = " OR ".join(f"${a}" for a in self.assets[:5])
        query = f"({tickers}) -is:retweet lang:en"

        try:
            params = {
                "query": query,
                "max_results": "20",
                "tweet.fields": "created_at,public_metrics,author_id",
                "sort_order": "relevancy",
            }
            async with session.get("https://api.twitter.com/2/tweets/search/recent",
                                   params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await self._process_tweets(data.get("data", []))
                elif resp.status == 429:
                    log.debug("X rate limited, backing off")
                else:
                    log.warning("X search %d", resp.status)
        except Exception as e:
            log.warning("X search error: %s", e)

    async def _process_tweets(self, tweets: list[dict]):
        """Process tweets into signals."""
        for tweet in tweets:
            tweet_id = tweet.get("id", "")
            if tweet_id in self._seen_ids:
                continue
            self._seen_ids.add(tweet_id)
            # Trim seen IDs
            if len(self._seen_ids) > 5000:
                self._seen_ids = set(list(self._seen_ids)[-2500:])

            text = tweet.get("text", "")
            metrics = tweet.get("public_metrics", {})
            engagement = (metrics.get("like_count", 0) +
                         metrics.get("retweet_count", 0) * 2 +
                         metrics.get("reply_count", 0))

            tickers = extract_tickers(text)
            sentiment = simple_sentiment(text)
            is_alpha = engagement > 100 or any(
                a in text.lower() for a in ALPHA_ACCOUNTS
            )

            post = SocialPost(
                platform="x",
                author=tweet.get("author_id", ""),
                text=text,
                ts=time.time(),
                engagement=engagement,
                tickers=tickers,
                sentiment=sentiment,
                is_alpha=is_alpha,
            )

            # Publish raw feed
            await self.bus.publish("intelligence.x_feed", {
                "text": text[:500],
                "tickers": tickers,
                "sentiment": sentiment,
                "engagement": engagement,
                "is_alpha": is_alpha,
                "ts": post.ts,
            })

            # Generate signal for high-engagement alpha posts
            if is_alpha and tickers and abs(sentiment) > 0.3:
                for ticker in tickers:
                    if ticker in self.assets:
                        # Weight by engagement (log scale)
                        import math
                        eng_weight = min(math.log1p(engagement) / 10, 0.5)
                        signal = Signal(
                            agent_id="x_monitor",
                            asset=ticker,
                            direction="long" if sentiment > 0 else "short",
                            strength=min(abs(sentiment) * eng_weight * 2, 1.0),
                            confidence=min(eng_weight + 0.2, 0.7),
                            rationale=f"[X] {text[:100]}... (eng={engagement})",
                        )
                        await self.bus.publish("signal.social_alpha", signal)


# ---------------------------------------------------------------------------
# Discord Webhook Agent
# ---------------------------------------------------------------------------
class DiscordAgent:
    """Posts trade signals and alerts to Discord via webhooks."""

    def __init__(self, bus: Bus):
        self.bus = bus
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "")
        self.alpha_webhook = os.getenv("DISCORD_ALPHA_WEBHOOK", "")
        self._session: aiohttp.ClientSession | None = None
        self._enabled = bool(self.webhook_url)

        if self._enabled:
            bus.subscribe("notification.trade", self._on_trade)
            bus.subscribe("signal.social_alpha", self._on_alpha)
            log.info("Discord agent enabled")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def _on_trade(self, notif: dict):
        """Post trade notification to Discord."""
        asset = notif.get("asset", "?")
        side = notif.get("side", "?")
        price = notif.get("price", 0)
        pnl = notif.get("pnl")
        strategy = notif.get("strategy", "")

        color = 0x00FF00 if side == "buy" else 0xFF0000
        pnl_text = f"\nPnL: **${pnl:+.2f}**" if pnl is not None else ""

        embed = {
            "embeds": [{
                "title": f"{'BUY' if side == 'buy' else 'SELL'} {asset}",
                "description": (
                    f"Price: **${price:,.2f}**{pnl_text}\n"
                    f"Strategy: {strategy}"
                ),
                "color": color,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "footer": {"text": "SwarmTrader"},
            }],
        }

        await self._post_webhook(self.webhook_url, embed)

    async def _on_alpha(self, signal: Signal):
        """Post alpha signal to Discord."""
        if not self.alpha_webhook:
            return
        embed = {
            "embeds": [{
                "title": f"ALPHA: {signal.direction.upper()} {signal.asset}",
                "description": signal.rationale[:500],
                "color": 0xFFD700,
                "fields": [
                    {"name": "Strength", "value": f"{signal.strength:.2f}", "inline": True},
                    {"name": "Confidence", "value": f"{signal.confidence:.2f}", "inline": True},
                ],
                "footer": {"text": "SwarmTrader Alpha"},
            }],
        }
        await self._post_webhook(self.alpha_webhook, embed)

    async def _post_webhook(self, url: str, payload: dict):
        if not url:
            return
        session = await self._get_session()
        try:
            async with session.post(url, json=payload) as resp:
                if resp.status not in (200, 204):
                    log.warning("Discord webhook %d", resp.status)
        except Exception as e:
            log.warning("Discord error: %s", e)

    async def post_message(self, text: str, webhook_url: str | None = None):
        """Post a plain text message to Discord."""
        url = webhook_url or self.webhook_url
        if not url:
            return
        await self._post_webhook(url, {"content": text})

    async def stop(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------
# Telegram Bot Agent
# ---------------------------------------------------------------------------
class TelegramAgent:
    """Sends trade alerts and signals via Telegram bot."""

    def __init__(self, bus: Bus):
        self.bus = bus
        self.bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self._session: aiohttp.ClientSession | None = None
        self._enabled = bool(self.bot_token and self.chat_id)

        if self._enabled:
            bus.subscribe("notification.trade", self._on_trade)
            bus.subscribe("intelligence.backtest", self._on_backtest)
            log.info("Telegram agent enabled (chat=%s)", self.chat_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def _on_trade(self, notif: dict):
        """Send trade notification via Telegram."""
        asset = notif.get("asset", "?")
        side = notif.get("side", "?")
        price = notif.get("price", 0)
        pnl = notif.get("pnl")
        emoji = "\U0001f7e2" if side == "buy" else "\U0001f534"
        pnl_line = f"\nPnL: *${pnl:+.2f}*" if pnl is not None else ""

        text = (
            f"{emoji} *{side.upper()} {asset}*\n"
            f"Price: ${price:,.2f}{pnl_line}\n"
            f"_{notif.get('strategy', '')}_"
        )
        await self.send_message(text, parse_mode="Markdown")

    async def _on_backtest(self, data: dict):
        """Send backtest summary."""
        text = (
            f"\U0001f4ca *Backtest Complete: {data.get('asset', '?')}*\n"
            f"Best: {data.get('best_strategy', '?')} (Sharpe={data.get('best_sharpe', 0):.2f})\n"
            f"Tested: {data.get('strategies_tested', 0)} | "
            f"Validated: {data.get('strategies_validated', 0)}"
        )
        await self.send_message(text, parse_mode="Markdown")

    async def send_message(self, text: str, parse_mode: str = "Markdown"):
        """Send a message via Telegram bot."""
        if not self._enabled:
            return
        session = await self._get_session()
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            async with session.post(url, json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }) as resp:
                if resp.status != 200:
                    log.warning("Telegram %d", resp.status)
        except Exception as e:
            log.warning("Telegram error: %s", e)

    async def stop(self):
        if self._session and not self._session.closed:
            await self._session.close()


# ---------------------------------------------------------------------------
# Social Aggregator — combines all social channels
# ---------------------------------------------------------------------------
class SocialAggregator:
    """Aggregates signals from all social channels into unified intelligence.

    Listens to:
      - signal.social_alpha (X/Twitter)
      - intelligence.x_feed
      - intelligence.birdeye (token analytics)

    Publishes:
      - signal.social_consensus — consensus signal from all social sources
    """

    def __init__(self, bus: Bus, assets: list[str] | None = None):
        self.bus = bus
        self.assets = set(assets or ["BTC", "ETH", "SOL"])
        self._signals: dict[str, list[Signal]] = {}
        self._window = 300  # 5-minute window for consensus

        bus.subscribe("signal.social_alpha", self._on_signal)

    async def _on_signal(self, signal: Signal):
        """Collect social signals and check for consensus."""
        if signal.asset not in self.assets:
            return

        signals = self._signals.setdefault(signal.asset, [])
        signals.append(signal)

        # Trim old signals
        cutoff = time.time() - self._window
        self._signals[signal.asset] = [s for s in signals if s.ts > cutoff]

        # Check for consensus (3+ signals in same direction)
        recent = self._signals[signal.asset]
        if len(recent) >= 3:
            long_count = sum(1 for s in recent if s.direction == "long")
            short_count = sum(1 for s in recent if s.direction == "short")
            total = len(recent)

            if long_count / total > 0.7:
                avg_strength = sum(s.strength for s in recent if s.direction == "long") / long_count
                consensus = Signal(
                    agent_id="social_consensus",
                    asset=signal.asset,
                    direction="long",
                    strength=min(avg_strength, 0.8),
                    confidence=min(long_count / total, 0.85),
                    rationale=f"[Social] {long_count}/{total} sources bullish",
                )
                await self.bus.publish("signal.social_consensus", consensus)
            elif short_count / total > 0.7:
                avg_strength = sum(s.strength for s in recent if s.direction == "short") / short_count
                consensus = Signal(
                    agent_id="social_consensus",
                    asset=signal.asset,
                    direction="short",
                    strength=min(avg_strength, 0.8),
                    confidence=min(short_count / total, 0.85),
                    rationale=f"[Social] {short_count}/{total} sources bearish",
                )
                await self.bus.publish("signal.social_consensus", consensus)
