"""Liquidation level agent: estimates where liquidation clusters sit
based on funding rates, open interest, and price levels.

Price acts as a magnet toward liquidation clusters — market makers and
large players hunt stops at these levels. Knowing where clusters sit
lets us:
  1. Avoid entering positions that will get stopped out
  2. Anticipate cascading liquidations (price acceleration)
  3. Fade moves that overshoot past liquidation zones
"""
from __future__ import annotations
import asyncio, logging, math, time
from collections import deque
from .core import Bus, MarketSnapshot, Signal
from .kraken import _run_cli

log = logging.getLogger("swarm.liquidation")

# Common leverage levels used by traders
LEVERAGE_LEVELS = [2, 3, 5, 10, 20, 25, 50, 100]


class LiquidationAgent:
    """Estimates liquidation price clusters and signals when price
    approaches them.

    Method:
      1. Track recent price range to estimate where leveraged entries were made
      2. Calculate liquidation prices for common leverage levels
      3. Monitor OI changes to estimate position sizes at each level
      4. Signal when price approaches a cluster (potential cascade)

    Publishes signal.liquidation per asset.
    """

    name = "liquidation"

    def __init__(self, bus: Bus, asset: str = "ETH",
                 futures_symbol: str = "PF_ETHUSD",
                 window: int = 100, interval: float = 30.0):
        self.bus = bus
        self.asset = asset
        self.futures_symbol = futures_symbol
        self.interval = interval
        self._stop = False

        self.prices: deque[float] = deque(maxlen=window)
        self._latest_price: float = 0.0
        self._latest_funding: float = 0.0

        bus.subscribe("market.snapshot", self._on_snap)

    def stop(self):
        self._stop = True

    async def _on_snap(self, snap: MarketSnapshot):
        price = snap.prices.get(self.asset)
        if price:
            self.prices.append(price)
            self._latest_price = price

    async def run(self):
        log.info("LiquidationAgent starting: asset=%s", self.asset)
        while not self._stop:
            try:
                await self._analyze()
            except Exception as e:
                log.debug("LiquidationAgent error: %s", e)
            await asyncio.sleep(self.interval)

    async def _analyze(self):
        if len(self.prices) < 20 or self._latest_price <= 0:
            return

        prices = list(self.prices)
        current = self._latest_price

        # Fetch funding rate to estimate market bias
        try:
            data = await _run_cli("futures", "tickers")
            for t in data.get("tickers", []):
                if t.get("symbol", "").upper() == self.futures_symbol.upper():
                    self._latest_funding = float(
                        t.get("relativeFundingRate", t.get("fundingRate", 0))
                    )
                    break
        except Exception as e:
            log.warning("Liquidation funding rate fetch failed: %s", e)

        # --- Estimate entry clusters ---
        # Use recent price levels as likely entry points
        # Weight recent prices more heavily (traders enter at recent levels)
        entry_levels = self._estimate_entry_levels(prices)

        # --- Calculate liquidation levels ---
        long_liqs = []   # (price_level, estimated_weight)
        short_liqs = []

        for entry, weight in entry_levels:
            for lev in LEVERAGE_LEVELS:
                # Long liquidation = entry * (1 - 1/leverage)
                long_liq = entry * (1 - 1 / lev)
                # Short liquidation = entry * (1 + 1/leverage)
                short_liq = entry * (1 + 1 / lev)

                # Higher leverage = more common for retail = more liquidations
                lev_weight = weight * math.log(lev + 1) / math.log(101)

                long_liqs.append((long_liq, lev_weight))
                short_liqs.append((short_liq, lev_weight))

        if not long_liqs:
            return

        # --- Find nearest clusters ---
        # Cluster liquidation levels into price zones (1% buckets)
        bucket_size = current * 0.01
        long_clusters = self._cluster_levels(long_liqs, bucket_size)
        short_clusters = self._cluster_levels(short_liqs, bucket_size)

        # Find nearest cluster below (long liquidations) and above (short liquidations)
        nearest_long_liq = None
        nearest_long_weight = 0.0
        for level, weight in long_clusters:
            if level < current:
                dist = (current - level) / current
                if dist < 0.10:  # within 10%
                    if nearest_long_liq is None or dist < (current - nearest_long_liq) / current:
                        nearest_long_liq = level
                        nearest_long_weight = weight

        nearest_short_liq = None
        nearest_short_weight = 0.0
        for level, weight in short_clusters:
            if level > current:
                dist = (level - current) / current
                if dist < 0.10:
                    if nearest_short_liq is None or dist < (nearest_short_liq - current) / current:
                        nearest_short_liq = level
                        nearest_short_weight = weight

        # --- Generate signal ---
        strength = 0.0
        rationale_parts = []

        # Funding rate bias: positive funding = more longs = more long liquidation risk
        if self._latest_funding > 0.0001:
            # Market is long-biased → long liquidation cluster is more dangerous
            if nearest_long_liq:
                dist_pct = (current - nearest_long_liq) / current
                if dist_pct < 0.03:  # within 3% of long liq cluster
                    strength = -min(0.8, (0.03 - dist_pct) * 30)
                    rationale_parts.append(
                        f"long_liq_near={nearest_long_liq:.0f} ({dist_pct:.1%} away)")
        elif self._latest_funding < -0.0001:
            # Market is short-biased → short squeeze risk
            if nearest_short_liq:
                dist_pct = (nearest_short_liq - current) / current
                if dist_pct < 0.03:
                    strength = min(0.8, (0.03 - dist_pct) * 30)
                    rationale_parts.append(
                        f"short_liq_near={nearest_short_liq:.0f} ({dist_pct:.1%} away)")

        # Magnetic effect: price tends to gravitate toward liquidation clusters
        if nearest_long_liq and nearest_short_liq:
            dist_down = (current - nearest_long_liq) / current
            dist_up = (nearest_short_liq - current) / current

            if dist_down < dist_up and nearest_long_weight > nearest_short_weight:
                # Closer to long liquidations with more weight → bearish magnet
                magnetic = -min(0.3, nearest_long_weight * 0.1)
                strength += magnetic
                rationale_parts.append(f"magnet_down w={nearest_long_weight:.1f}")
            elif dist_up < dist_down and nearest_short_weight > nearest_long_weight:
                magnetic = min(0.3, nearest_short_weight * 0.1)
                strength += magnetic
                rationale_parts.append(f"magnet_up w={nearest_short_weight:.1f}")

        strength = max(-1.0, min(1.0, strength))
        if abs(strength) < 0.05:
            return

        confidence = min(1.0, 0.3 + abs(self._latest_funding) * 1000)
        rationale_parts.append(f"funding={self._latest_funding:+.6f}")

        sig = Signal(
            self.name, self.asset,
            "long" if strength > 0 else "short",
            strength, confidence,
            " ".join(rationale_parts),
        )
        await self.bus.publish("signal.liquidation", sig)

    def _estimate_entry_levels(self, prices: list[float]) -> list[tuple[float, float]]:
        """Estimate where traders likely entered positions.
        Returns [(price_level, weight)] with recent prices weighted higher."""
        entries = []
        n = len(prices)
        for i, p in enumerate(prices):
            # Recency weight: more recent = higher weight
            recency = (i + 1) / n
            entries.append((p, recency))
        return entries

    def _cluster_levels(self, levels: list[tuple[float, float]],
                        bucket_size: float) -> list[tuple[float, float]]:
        """Group nearby levels into clusters. Returns [(avg_price, total_weight)]."""
        if not levels:
            return []

        sorted_levels = sorted(levels, key=lambda x: x[0])
        clusters = []
        current_bucket = [sorted_levels[0]]

        for level, weight in sorted_levels[1:]:
            if level - current_bucket[0][0] < bucket_size:
                current_bucket.append((level, weight))
            else:
                # Close current bucket
                total_w = sum(w for _, w in current_bucket)
                avg_p = sum(p * w for p, w in current_bucket) / total_w if total_w > 0 else current_bucket[0][0]
                clusters.append((avg_p, total_w))
                current_bucket = [(level, weight)]

        # Close last bucket
        if current_bucket:
            total_w = sum(w for _, w in current_bucket)
            avg_p = sum(p * w for p, w in current_bucket) / total_w if total_w > 0 else current_bucket[0][0]
            clusters.append((avg_p, total_w))

        return clusters
