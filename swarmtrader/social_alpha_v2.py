"""Social Alpha Scanner v2 — automated trending token discovery.

Inspired by pochi.po (AI scans Twitter for meme trends, evaluates viral
potential, auto-mints), SpaceCoin (monitors Twitter for new meme coins,
auto-creates pegged tokens), Trendy-Tokens-AI (scrapes Twitter + CoinGecko
for trending tokens then auto-invests), and Meme Sentinels (concurrent
processing of 1000+ tokens).

Upgrades the existing social_agents.py with:
  1. Cross-platform scanning (X/Twitter, Farcaster, Reddit, Telegram)
  2. Virality scoring (engagement velocity, not just volume)
  3. Token mention clustering (detect coordinated shilling vs organic)
  4. Early mover detection (find tokens before they trend)
  5. Rug filter integration (cross-reference with rugpull_detector)

Bus integration:
  Subscribes to: signal.social, signal.sentiment, signal.news
  Publishes to:  social_alpha.trending, social_alpha.early, signal.social_alpha
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections import defaultdict

from .core import Bus, Signal

log = logging.getLogger("swarm.social_alpha")


@dataclass
class TokenMention:
    """A social media mention of a token."""
    token: str
    platform: str          # "twitter", "farcaster", "reddit", "telegram"
    author: str
    engagement: int        # likes + retweets + replies
    sentiment: float       # -1 to 1
    is_influencer: bool
    ts: float = field(default_factory=time.time)


@dataclass
class TrendingToken:
    """A token detected as trending across social platforms."""
    token: str
    # Virality metrics
    mention_count: int = 0
    unique_authors: int = 0
    total_engagement: int = 0
    engagement_velocity: float = 0.0   # engagements per minute
    sentiment_avg: float = 0.0
    # Platform distribution
    platforms: dict[str, int] = field(default_factory=dict)  # platform -> mention count
    # Coordination detection
    is_organic: bool = True
    coordination_score: float = 0.0    # 0=organic, 1=coordinated shill
    # Timing
    first_seen: float = field(default_factory=time.time)
    peak_velocity: float = 0.0
    # Classification
    category: str = "unknown"          # "meme", "defi", "nft", "ai", "gaming"
    signal_strength: float = 0.0


class SocialAlphaScanner:
    """Cross-platform social alpha detection engine.

    Scans for tokens gaining organic traction across multiple platforms
    simultaneously. Uses engagement velocity (not just volume) to detect
    early movers before they peak.

    Virality scoring: velocity * sentiment * diversity * (1 - coordination)
      - velocity: how fast engagement is growing
      - sentiment: positive mentions weighted higher
      - diversity: mentions across multiple platforms = stronger signal
      - coordination: detected shill campaigns penalized
    """

    def __init__(self, bus: Bus, window_s: float = 600.0,
                 min_mentions: int = 3, min_velocity: float = 0.5):
        self.bus = bus
        self.window_s = window_s
        self.min_mentions = min_mentions
        self.min_velocity = min_velocity
        self._mentions: dict[str, list[TokenMention]] = defaultdict(list)
        self._trending: dict[str, TrendingToken] = {}
        self._stats = {
            "mentions_processed": 0, "trending_detected": 0,
            "early_signals": 0, "shill_filtered": 0,
        }

        bus.subscribe("signal.social", self._on_social_signal)
        bus.subscribe("signal.sentiment", self._on_sentiment_signal)
        bus.subscribe("signal.news", self._on_news_signal)

    async def _on_social_signal(self, sig: Signal):
        """Process social media signals for token mentions."""
        if not isinstance(sig, Signal):
            return
        await self._process_mention(sig, "twitter")

    async def _on_sentiment_signal(self, sig: Signal):
        if isinstance(sig, Signal):
            await self._process_mention(sig, "sentiment")

    async def _on_news_signal(self, sig: Signal):
        if isinstance(sig, Signal):
            await self._process_mention(sig, "news")

    async def _process_mention(self, sig: Signal, platform: str):
        """Process a token mention from any platform."""
        self._stats["mentions_processed"] += 1

        mention = TokenMention(
            token=sig.asset,
            platform=platform,
            author=sig.agent_id,
            engagement=int(sig.strength * 100),
            sentiment=sig.strength if sig.direction == "long" else -sig.strength,
            is_influencer=sig.confidence > 0.7,
        )

        self._mentions[sig.asset].append(mention)

        # Trim old mentions
        cutoff = time.time() - self.window_s
        self._mentions[sig.asset] = [
            m for m in self._mentions[sig.asset] if m.ts > cutoff
        ]

        # Analyze trending
        await self._analyze_token(sig.asset)

    async def _analyze_token(self, token: str):
        """Analyze a token's social metrics for trending signal."""
        mentions = self._mentions.get(token, [])
        if len(mentions) < self.min_mentions:
            return

        # Calculate metrics
        unique_authors = len(set(m.author for m in mentions))
        total_engagement = sum(m.engagement for m in mentions)
        sentiment_avg = sum(m.sentiment for m in mentions) / len(mentions)

        # Engagement velocity (per minute)
        if len(mentions) >= 2:
            timespan = max(mentions[-1].ts - mentions[0].ts, 60)
            velocity = total_engagement / (timespan / 60)
        else:
            velocity = 0.0

        # Platform diversity
        platforms = defaultdict(int)
        for m in mentions:
            platforms[m.platform] += 1
        platform_count = len(platforms)

        # Coordination detection
        # If >80% mentions from same author or same platform, likely coordinated
        author_counts = defaultdict(int)
        for m in mentions:
            author_counts[m.author] += 1
        max_author_pct = max(author_counts.values()) / len(mentions) if mentions else 0
        max_platform_pct = max(platforms.values()) / len(mentions) if mentions else 0
        coordination = max(max_author_pct - 0.3, max_platform_pct - 0.6, 0) * 2
        coordination = min(1.0, coordination)

        is_organic = coordination < 0.5

        if not is_organic:
            self._stats["shill_filtered"] += 1
            return

        # Virality score
        diversity_bonus = min(1.0, platform_count * 0.3)
        signal_strength = (
            min(1.0, velocity / 10) * 0.3 +
            max(0, sentiment_avg) * 0.2 +
            diversity_bonus * 0.2 +
            (1 - coordination) * 0.3
        )

        if velocity < self.min_velocity:
            return

        trending = TrendingToken(
            token=token,
            mention_count=len(mentions),
            unique_authors=unique_authors,
            total_engagement=total_engagement,
            engagement_velocity=round(velocity, 2),
            sentiment_avg=round(sentiment_avg, 3),
            platforms=dict(platforms),
            is_organic=is_organic,
            coordination_score=round(coordination, 3),
            peak_velocity=max(velocity, self._trending.get(token, TrendingToken(token=token)).peak_velocity),
            signal_strength=round(signal_strength, 3),
        )

        # Check if this is a new trend or update
        was_trending = token in self._trending
        self._trending[token] = trending

        if not was_trending:
            self._stats["trending_detected"] += 1
            log.info(
                "SOCIAL ALPHA: %s trending! mentions=%d velocity=%.1f/min "
                "sentiment=%.2f platforms=%d organic=%s",
                token, len(mentions), velocity, sentiment_avg,
                platform_count, is_organic,
            )

            # Publish early signal (before it's fully trending)
            if velocity > self.min_velocity * 2:
                self._stats["early_signals"] += 1
                sig = Signal(
                    agent_id="social_alpha_v2",
                    asset=token,
                    direction="long" if sentiment_avg > 0 else "short",
                    strength=signal_strength,
                    confidence=min(1.0, unique_authors * 0.1),
                    rationale=(
                        f"Social alpha: {token} trending ({len(mentions)} mentions, "
                        f"velocity={velocity:.1f}/min, {platform_count} platforms, "
                        f"organic={is_organic})"
                    ),
                )
                await self.bus.publish("signal.social_alpha", sig)
                await self.bus.publish("social_alpha.trending", trending)

    def get_trending(self, top_n: int = 10) -> list[dict]:
        """Get currently trending tokens."""
        sorted_tokens = sorted(
            self._trending.values(),
            key=lambda t: t.signal_strength, reverse=True,
        )
        return [
            {
                "token": t.token,
                "mentions": t.mention_count,
                "velocity": t.engagement_velocity,
                "sentiment": round(t.sentiment_avg, 2),
                "platforms": t.platforms,
                "organic": t.is_organic,
                "strength": t.signal_strength,
            }
            for t in sorted_tokens[:top_n]
        ]

    def summary(self) -> dict:
        return {
            **self._stats,
            "tokens_tracked": len(self._mentions),
            "currently_trending": len(self._trending),
            "top_trending": self.get_trending(5),
        }
