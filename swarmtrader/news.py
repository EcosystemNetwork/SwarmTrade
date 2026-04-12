"""News sentiment agent: polls CryptoPanic API for crypto news and derives trading signals.

Pipeline: Raw headlines → keyword urgency scoring → vote sentiment → recency weighting → Signal
"""
from __future__ import annotations
import asyncio, logging, os, re
from collections import deque
from dataclasses import dataclass
import aiohttp
from .core import Bus, Signal

log = logging.getLogger("swarm.news")

CRYPTOPANIC_BASE = "https://cryptopanic.com/api/free/v1"


# ── Keyword impact tables ──────────────────────────────────────────
# Each keyword maps to a (direction_bias, magnitude_boost) tuple.
# direction_bias: +1 bullish, -1 bearish, 0 context-dependent
# magnitude_boost: multiplier applied to the article's sentiment weight

BULLISH_KEYWORDS: dict[str, tuple[float, float]] = {
    # Adoption / institutional
    r"\betf\b.*approv":       (+1.0, 2.5),
    r"\betf\b.*launch":       (+1.0, 2.0),
    r"\betf\b.*fil":          (+1.0, 2.0),
    r"institutional.*buy":    (+1.0, 1.8),
    r"mass\s*adoption":       (+1.0, 1.5),
    r"partnershi":            (+1.0, 1.3),
    r"integrat":              (+1.0, 1.2),
    # Supply / scarcity
    r"halving":               (+1.0, 1.5),
    r"supply\s*shock":        (+1.0, 1.8),
    r"token\s*burn":          (+1.0, 1.4),
    # Regulatory positive
    r"legal\s*tender":        (+1.0, 2.0),
    r"regulat.*clar":         (+1.0, 1.5),
    r"regulat.*framework":    (+1.0, 1.3),
    # Market structure
    r"all.time.high":         (+1.0, 1.6),
    r"\bath\b":               (+1.0, 1.6),
    r"breakout":              (+1.0, 1.3),
    r"rally":                 (+1.0, 1.2),
    r"surge[sd]?":            (+1.0, 1.3),
    r"soar":                  (+1.0, 1.3),
}

BEARISH_KEYWORDS: dict[str, tuple[float, float]] = {
    # Security incidents
    r"hack(ed|s|ing)?":       (-1.0, 2.5),
    r"exploit(ed|s)?":        (-1.0, 2.5),
    r"rug\s*pull":            (-1.0, 2.5),
    r"drain(ed)?":            (-1.0, 2.0),
    r"vulnerabilit":          (-1.0, 1.8),
    r"breach":                (-1.0, 2.0),
    # Regulatory negative
    r"\bban(ned|s)?\b":       (-1.0, 2.2),
    r"crackdown":             (-1.0, 2.0),
    r"lawsuit":               (-1.0, 1.8),
    r"sec\s*(su|charg|fine)": (-1.0, 2.0),
    r"sanction":              (-1.0, 1.8),
    r"fraud":                 (-1.0, 2.0),
    # Market negative
    r"crash(ed|es|ing)?":     (-1.0, 2.0),
    r"plunge[sd]?":           (-1.0, 1.8),
    r"dump(ed|ing)?":         (-1.0, 1.5),
    r"liquidat":              (-1.0, 1.8),
    r"insolvency|insolvent":  (-1.0, 2.2),
    r"bankrupt":              (-1.0, 2.2),
    r"delisted?":             (-1.0, 1.8),
    r"ponzi":                 (-1.0, 2.0),
}


@dataclass
class _ScoredArticle:
    """Intermediate scoring result for a single article."""
    title: str
    sentiment: float       # -1..1
    weight: float          # recency + urgency combined
    urgency: float         # 0..1 how time-sensitive this headline is
    keywords_hit: list[str]


class NewsAgent:
    """Fetches crypto news from CryptoPanic and converts sentiment into trading signals.

    Pipeline:
    1. Poll CryptoPanic for recent headlines per tracked asset
    2. Score each article via votes + keyword urgency analysis
    3. Weight by recency (newer = higher) and urgency (high-impact = higher)
    4. Aggregate into a single directional Signal per asset
    5. Publish signal.news onto the Bus

    Requires a free API key from https://cryptopanic.com/developers/api/
    Set via NEWS_API_KEY environment variable.
    """

    name = "news"

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 60.0, lookback: int = 10,
                 urgency_threshold: float = 0.5):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self.lookback = lookback
        self.urgency_threshold = urgency_threshold
        self.api_key = os.getenv("NEWS_API_KEY", "")
        self._stop = False
        self._seen: deque[str] = deque(maxlen=200)
        # Compile keyword patterns once
        self._bull_patterns = [(re.compile(p, re.IGNORECASE), v) for p, v in BULLISH_KEYWORDS.items()]
        self._bear_patterns = [(re.compile(p, re.IGNORECASE), v) for p, v in BEARISH_KEYWORDS.items()]

    def stop(self):
        self._stop = True

    async def run(self):
        if not self.api_key:
            log.warning("NEWS_API_KEY not set, NewsAgent disabled")
            return
        log.info("NewsAgent starting: assets=%s interval=%.0fs", self.assets, self.interval)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                for asset in self.assets:
                    await self._fetch_and_signal(session, asset)
                await asyncio.sleep(self.interval)

    # ── Core pipeline ──────────────────────────────────────────────

    async def _fetch_and_signal(self, session: aiohttp.ClientSession, asset: str):
        articles_raw = await self._fetch_articles(session, asset)
        if not articles_raw:
            return

        scored = self._score_articles(articles_raw)
        signal = self._aggregate_signal(asset, scored)
        if signal:
            await self.bus.publish("signal.news", signal)

    async def _fetch_articles(self, session: aiohttp.ClientSession, asset: str) -> list[dict]:
        currency = _asset_to_currency(asset)
        url = f"{CRYPTOPANIC_BASE}/posts/"
        params = {
            "auth_token": self.api_key,
            "currencies": currency,
            "kind": "news",
            "public": "true",
        }
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.debug("CryptoPanic %s returned %d", asset, resp.status)
                    return []
                data = await resp.json()
        except Exception as e:
            log.debug("CryptoPanic fetch failed for %s: %s", asset, e)
            return []

        return data.get("results", [])[:self.lookback]

    def _score_articles(self, articles: list[dict]) -> list[_ScoredArticle]:
        """Score each article using votes + keyword analysis."""
        scored = []
        for i, article in enumerate(articles):
            title = article.get("title", "")
            article_url = article.get("url", "")

            # Track seen articles
            is_new = False
            if article_url and article_url not in self._seen:
                self._seen.append(article_url)
                is_new = True

            # Vote-based sentiment
            vote_sentiment = self._vote_sentiment(article)

            # Keyword-based sentiment and urgency
            kw_bias, kw_boost, urgency, kw_hits = self._keyword_analysis(title)

            # Combine: votes are primary, keywords adjust
            if abs(vote_sentiment) > 0.01:
                sentiment = vote_sentiment
                # Keywords can amplify vote sentiment in same direction
                if kw_bias * vote_sentiment > 0:
                    sentiment *= (1.0 + (kw_boost - 1.0) * 0.5)
                # Keywords opposing votes dampen
                elif abs(kw_bias) > 0.01:
                    sentiment *= 0.7
            elif abs(kw_bias) > 0.01:
                # No votes — keyword analysis drives sentiment
                sentiment = kw_bias * min(1.0, kw_boost * 0.4)
            else:
                # Fall back to CryptoPanic's own sentiment tag
                sentiment = _kind_to_score(article.get("kind", "news"))

            sentiment = max(-1.0, min(1.0, sentiment))

            # Recency weight: newer articles matter more
            recency_w = 1.0 / (1 + i * 0.3)
            # New articles get a freshness boost
            freshness = 1.3 if is_new else 1.0
            # Urgency boost for high-impact keywords
            urgency_w = 1.0 + urgency * 1.5

            weight = recency_w * freshness * urgency_w

            scored.append(_ScoredArticle(
                title=title,
                sentiment=sentiment,
                weight=weight,
                urgency=urgency,
                keywords_hit=kw_hits,
            ))
        return scored

    def _vote_sentiment(self, article: dict) -> float:
        """Extract sentiment from CryptoPanic community votes."""
        votes = article.get("votes", {})
        positive = votes.get("positive", 0)
        negative = votes.get("negative", 0)
        important = votes.get("important", 0)
        liked = votes.get("liked", 0)
        disliked = votes.get("disliked", 0)

        bull = positive + liked + important * 0.5
        bear = negative + disliked
        total = bull + bear
        if total > 0:
            return (bull - bear) / total
        return 0.0

    def _keyword_analysis(self, title: str) -> tuple[float, float, float, list[str]]:
        """Scan title for high-impact keywords.

        Returns (direction_bias, magnitude_boost, urgency, keywords_hit).
        """
        if not title:
            return 0.0, 1.0, 0.0, []

        bull_score = 0.0
        bear_score = 0.0
        max_boost = 1.0
        hits: list[str] = []

        for pat, (bias, boost) in self._bull_patterns:
            if pat.search(title):
                bull_score += bias * boost
                max_boost = max(max_boost, boost)
                hits.append(pat.pattern[:20])

        for pat, (bias, boost) in self._bear_patterns:
            if pat.search(title):
                bear_score += abs(bias) * boost
                max_boost = max(max_boost, boost)
                hits.append(pat.pattern[:20])

        net = bull_score - bear_score
        if abs(net) < 0.01:
            return 0.0, 1.0, 0.0, hits

        direction = 1.0 if net > 0 else -1.0
        # Urgency: how impactful are the matched keywords (0..1)
        urgency = min(1.0, max(0.0, (max_boost - 1.0) / 1.5))

        return direction, max_boost, urgency, hits

    def _aggregate_signal(self, asset: str, scored: list[_ScoredArticle]) -> Signal | None:
        """Combine scored articles into a single directional signal."""
        if not scored:
            return None

        total_weight = sum(a.weight for a in scored)
        if total_weight < 1e-9:
            return None

        weighted_sentiment = sum(a.sentiment * a.weight for a in scored) / total_weight
        strength = max(-1.0, min(1.0, weighted_sentiment))

        # Filter out noise: require minimum signal strength
        if abs(strength) < 0.05:
            return None

        # Confidence: more articles + higher urgency = more confident
        article_coverage = min(1.0, len(scored) / self.lookback)
        max_urgency = max(a.urgency for a in scored)
        # High-urgency keywords boost confidence
        confidence = min(1.0, article_coverage * 0.6 + 0.2 + max_urgency * 0.2)

        # Build rationale
        new_count = sum(1 for a in scored if a.weight > 1.2)  # freshness-boosted
        top_headlines = [a.title[:60] for a in scored[:3]]
        all_kw = []
        for a in scored:
            all_kw.extend(a.keywords_hit)
        kw_summary = ",".join(dict.fromkeys(all_kw))[:80]  # dedupe, truncate

        parts = [f"news={strength:+.3f}", f"n={len(scored)}", f"new={new_count}"]
        if kw_summary:
            parts.append(f"kw=[{kw_summary}]")
        if max_urgency >= self.urgency_threshold:
            parts.append(f"URGENT({max_urgency:.1f})")
        parts.append("|")
        parts.append("; ".join(top_headlines))
        rationale = " ".join(parts)

        direction: str = "long" if strength > 0 else "short"

        return Signal(self.name, asset, direction, strength, confidence, rationale)


def _asset_to_currency(asset: str) -> str:
    """Map our asset symbols to CryptoPanic currency codes."""
    mapping = {
        "BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XRP": "XRP",
        "ADA": "ADA", "DOT": "DOT", "LINK": "LINK", "AVAX": "AVAX",
        "DOGE": "DOGE", "MATIC": "MATIC", "LTC": "LTC",
    }
    return mapping.get(asset.upper(), asset.upper())


def _kind_to_score(kind: str) -> float:
    """Fallback sentiment from CryptoPanic's article kind tag."""
    return {
        "bullish": 0.5,
        "bearish": -0.5,
        "important": 0.2,
        "lol": -0.1,
        "news": 0.0,
    }.get(kind.lower(), 0.0)
