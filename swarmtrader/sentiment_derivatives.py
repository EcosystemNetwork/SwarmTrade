"""Sentiment Derivatives — trade sentiment as a quantifiable asset.

Inspired by PortfolAI (market sentiment through emotional social media
analysis), PatagonAI (prediction markets for forecasts), and the idea
that sentiment itself has tradeable dynamics (momentum, mean reversion).

Creates synthetic "sentiment indices" per asset that can be:
  1. Tracked like a price (sentiment level over time)
  2. Analyzed with TA indicators (RSI of sentiment, sentiment MACD)
  3. Used for divergence signals (price up + sentiment down = bearish)
  4. Converted to prediction market positions

Bus integration:
  Subscribes to: signal.sentiment, signal.social, signal.news, signal.fear_greed
  Publishes to:  sentiment.index, sentiment.divergence, signal.sentiment_deriv
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from collections import deque

from .core import Bus, Signal

log = logging.getLogger("swarm.sent_deriv")


@dataclass
class SentimentIndex:
    """A synthetic sentiment index for an asset."""
    asset: str
    # Current levels
    level: float = 0.0           # -1 (extreme fear) to +1 (extreme greed)
    velocity: float = 0.0        # rate of change
    # Technical indicators on sentiment itself
    rsi: float = 50.0            # RSI of sentiment (0-100)
    ema_fast: float = 0.0        # 12-period EMA
    ema_slow: float = 0.0        # 26-period EMA
    macd: float = 0.0            # fast - slow
    macd_signal: float = 0.0     # 9-period EMA of MACD
    # Divergence
    price_direction: str = ""     # "up" or "down"
    sentiment_direction: str = "" # "up" or "down"
    is_divergent: bool = False    # price and sentiment disagree
    divergence_strength: float = 0.0
    # History
    history: deque = field(default_factory=lambda: deque(maxlen=200))
    last_update: float = 0.0


class SentimentDerivativesEngine:
    """Treats sentiment as a quantifiable, analyzable asset.

    Aggregates sentiment data from all sources into per-asset indices,
    then applies traditional TA indicators to the sentiment itself.

    Key signals:
      - Sentiment RSI overbought/oversold (crowd too bullish/bearish)
      - Sentiment MACD crossover (sentiment momentum shift)
      - Price/sentiment divergence (price up but sentiment falling)
      - Sentiment velocity spike (sudden shift in crowd opinion)
    """

    def __init__(self, bus: Bus, divergence_threshold: float = 0.3):
        self.bus = bus
        self.divergence_threshold = divergence_threshold
        self._indices: dict[str, SentimentIndex] = {}
        self._prices: dict[str, deque] = {}
        self._stats = {"updates": 0, "divergences": 0, "signals": 0}

        bus.subscribe("signal.sentiment", self._on_sentiment)
        bus.subscribe("signal.social", self._on_sentiment)
        bus.subscribe("signal.news", self._on_sentiment)
        bus.subscribe("signal.fear_greed", self._on_fear_greed)
        bus.subscribe("market.snapshot", self._on_snapshot)

    def _get_index(self, asset: str) -> SentimentIndex:
        if asset not in self._indices:
            self._indices[asset] = SentimentIndex(asset=asset)
        return self._indices[asset]

    async def _on_snapshot(self, snap):
        """Track prices for divergence detection."""
        for asset, price in snap.prices.items():
            if asset not in self._prices:
                self._prices[asset] = deque(maxlen=100)
            self._prices[asset].append(price)

    async def _on_fear_greed(self, sig: Signal):
        """Process Fear & Greed Index as global sentiment."""
        if isinstance(sig, Signal):
            # F&G is 0-100, normalize to -1..1
            normalized = (sig.strength * 2) - 1  # assuming strength is 0-1
            for asset in self._indices:
                idx = self._get_index(asset)
                # Blend global sentiment with asset-specific
                idx.level = idx.level * 0.7 + normalized * 0.3

    async def _on_sentiment(self, sig: Signal):
        """Process sentiment signal and update index."""
        if not isinstance(sig, Signal):
            return

        idx = self._get_index(sig.asset)
        self._stats["updates"] += 1

        # Convert to sentiment score (-1 to 1)
        score = sig.strength * sig.confidence
        if sig.direction == "short":
            score = -score

        # Update level with EMA smoothing
        alpha = 0.2
        old_level = idx.level
        idx.level = idx.level * (1 - alpha) + score * alpha
        idx.velocity = idx.level - old_level

        # Record history
        idx.history.append((time.time(), idx.level))
        idx.last_update = time.time()

        # Update technical indicators on sentiment
        self._update_technicals(idx)

        # Check for divergence with price
        await self._check_divergence(idx)

        # Generate signal if indicators are extreme
        await self._check_extremes(idx)

    def _update_technicals(self, idx: SentimentIndex):
        """Apply TA indicators to the sentiment index."""
        levels = [level for _, level in idx.history]
        if len(levels) < 14:
            return

        # EMA
        idx.ema_fast = self._ema(levels, 12)
        idx.ema_slow = self._ema(levels, 26)
        idx.macd = idx.ema_fast - idx.ema_slow

        # RSI of sentiment
        idx.rsi = self._rsi(levels, 14)

    def _ema(self, data: list[float], period: int) -> float:
        """Exponential Moving Average."""
        if len(data) < period:
            return sum(data) / len(data) if data else 0
        alpha = 2 / (period + 1)
        ema = data[0]
        for val in data[1:]:
            ema = alpha * val + (1 - alpha) * ema
        return ema

    def _rsi(self, data: list[float], period: int = 14) -> float:
        """Relative Strength Index of sentiment."""
        if len(data) < period + 1:
            return 50.0
        changes = [data[i] - data[i - 1] for i in range(1, len(data))]
        recent = changes[-period:]
        gains = [c for c in recent if c > 0]
        losses = [-c for c in recent if c < 0]
        avg_gain = sum(gains) / period if gains else 0.001
        avg_loss = sum(losses) / period if losses else 0.001
        rs = avg_gain / max(avg_loss, 0.001)
        return 100 - (100 / (1 + rs))

    async def _check_divergence(self, idx: SentimentIndex):
        """Check for price/sentiment divergence."""
        prices = self._prices.get(idx.asset)
        if not prices or len(prices) < 10 or len(idx.history) < 10:
            return

        # Price direction (recent 10 ticks)
        price_list = list(prices)
        price_change = price_list[-1] - price_list[-10] if len(price_list) >= 10 else 0
        idx.price_direction = "up" if price_change > 0 else "down"

        # Sentiment direction (recent 10 entries)
        sent_list = [l for _, l in list(idx.history)[-10:]]
        sent_change = sent_list[-1] - sent_list[0] if len(sent_list) >= 2 else 0
        idx.sentiment_direction = "up" if sent_change > 0 else "down"

        # Divergence: price and sentiment disagree
        was_divergent = idx.is_divergent
        idx.is_divergent = idx.price_direction != idx.sentiment_direction
        idx.divergence_strength = abs(price_change / max(abs(price_list[-1]), 1)) + abs(sent_change)

        if idx.is_divergent and idx.divergence_strength > self.divergence_threshold and not was_divergent:
            self._stats["divergences"] += 1
            # Divergence signal: trust sentiment over price
            direction = "short" if idx.sentiment_direction == "down" else "long"
            sig = Signal(
                agent_id="sentiment_derivatives",
                asset=idx.asset,
                direction=direction,
                strength=min(1.0, idx.divergence_strength),
                confidence=0.6,
                rationale=(
                    f"Divergence: price {idx.price_direction} but sentiment {idx.sentiment_direction} "
                    f"(strength={idx.divergence_strength:.2f}, sent_rsi={idx.rsi:.0f})"
                ),
            )
            await self.bus.publish("signal.sentiment_deriv", sig)
            await self.bus.publish("sentiment.divergence", {
                "asset": idx.asset, "divergence": idx.divergence_strength,
                "price_dir": idx.price_direction, "sent_dir": idx.sentiment_direction,
            })
            log.info(
                "SENTIMENT DIVERGENCE: %s price %s but sentiment %s (str=%.2f, RSI=%.0f)",
                idx.asset, idx.price_direction, idx.sentiment_direction,
                idx.divergence_strength, idx.rsi,
            )

    async def _check_extremes(self, idx: SentimentIndex):
        """Generate signals when sentiment indicators hit extremes."""
        if idx.rsi > 80:
            # Sentiment extremely bullish — contrarian short signal
            self._stats["signals"] += 1
            sig = Signal(
                agent_id="sentiment_derivatives",
                asset=idx.asset,
                direction="short",
                strength=min(1.0, (idx.rsi - 70) / 30),
                confidence=0.5,
                rationale=f"Sentiment RSI overbought at {idx.rsi:.0f} — crowd too bullish",
            )
            await self.bus.publish("signal.sentiment_deriv", sig)

        elif idx.rsi < 20:
            # Sentiment extremely bearish — contrarian long signal
            self._stats["signals"] += 1
            sig = Signal(
                agent_id="sentiment_derivatives",
                asset=idx.asset,
                direction="long",
                strength=min(1.0, (30 - idx.rsi) / 30),
                confidence=0.5,
                rationale=f"Sentiment RSI oversold at {idx.rsi:.0f} — crowd too bearish",
            )
            await self.bus.publish("signal.sentiment_deriv", sig)

    def summary(self) -> dict:
        return {
            **self._stats,
            "indices": {
                asset: {
                    "level": round(idx.level, 3),
                    "velocity": round(idx.velocity, 4),
                    "rsi": round(idx.rsi, 1),
                    "macd": round(idx.macd, 4),
                    "divergent": idx.is_divergent,
                    "price_dir": idx.price_direction,
                    "sent_dir": idx.sentiment_direction,
                }
                for asset, idx in self._indices.items()
            },
        }
