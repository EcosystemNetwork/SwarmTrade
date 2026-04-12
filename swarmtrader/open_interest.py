"""Open Interest agent: tracks OI changes on futures markets to detect
trend strength, crowded positioning, and liquidation risk.

OI rising + price rising  = strong trend (new longs entering)
OI rising + price falling = bearish pressure (new shorts entering)
OI falling + price rising = short squeeze (shorts closing)
OI falling + price falling = capitulation (longs closing)
"""
from __future__ import annotations
import asyncio, logging, time
from collections import deque
from .core import Bus, MarketSnapshot, Signal
from .kraken import _run_cli

log = logging.getLogger("swarm.oi")


class OpenInterestAgent:
    """Monitors futures open interest changes and cross-references with
    price action to produce directional signals.

    Uses Kraken futures API via CLI for OI data.

    Publishes signal.open_interest per asset.
    """

    name = "open_interest"

    def __init__(self, bus: Bus, symbol: str = "PF_ETHUSD",
                 asset: str = "ETH", interval: float = 60.0,
                 window: int = 30):
        self.bus = bus
        self.symbol = symbol
        self.asset = asset
        self.interval = interval
        self._stop = False

        # Rolling history
        self.oi_history: deque[float] = deque(maxlen=window)
        self.price_history: deque[float] = deque(maxlen=window)
        self._latest_price: float = 0.0

        bus.subscribe("market.snapshot", self._on_snap)

    def stop(self):
        self._stop = True

    async def _on_snap(self, snap: MarketSnapshot):
        price = snap.prices.get(self.asset)
        if price:
            self._latest_price = price

    async def run(self):
        log.info("OpenInterestAgent starting: symbol=%s", self.symbol)
        while not self._stop:
            try:
                await self._poll_oi()
            except Exception as e:
                log.debug("OpenInterestAgent error: %s", e)
            await asyncio.sleep(self.interval)

    async def _poll_oi(self):
        data = await _run_cli("futures", "tickers")
        tickers = data.get("tickers", [])

        for t in tickers:
            if t.get("symbol", "").upper() != self.symbol.upper():
                continue

            oi = t.get("openInterest", t.get("oi", None))
            if oi is None or not isinstance(oi, (int, float)):
                return

            oi = float(oi)
            self.oi_history.append(oi)

            if self._latest_price > 0:
                self.price_history.append(self._latest_price)

            if len(self.oi_history) < 3:
                return

            # Compute OI change rate
            oi_list = list(self.oi_history)
            oi_change_pct = (oi_list[-1] / oi_list[-2] - 1) if oi_list[-2] > 0 else 0
            oi_trend = (oi_list[-1] / oi_list[0] - 1) if oi_list[0] > 0 else 0

            # Compute price change (matching window)
            price_change = 0.0
            if len(self.price_history) >= 2:
                p = list(self.price_history)
                price_change = p[-1] / p[-2] - 1

            # Classify the OI+price regime
            strength = 0.0
            regime = ""

            if oi_change_pct > 0.005:  # OI rising (>0.5%)
                if price_change > 0.001:
                    # OI up + price up = strong bullish trend
                    strength = min(1.0, (oi_change_pct + price_change) * 20)
                    regime = "trend_long"
                elif price_change < -0.001:
                    # OI up + price down = bearish pressure / new shorts
                    strength = max(-1.0, -(oi_change_pct + abs(price_change)) * 20)
                    regime = "pressure_short"
            elif oi_change_pct < -0.005:  # OI falling
                if price_change > 0.001:
                    # OI down + price up = short squeeze
                    strength = min(1.0, (abs(oi_change_pct) + price_change) * 15)
                    regime = "short_squeeze"
                elif price_change < -0.001:
                    # OI down + price down = capitulation
                    strength = max(-1.0, (oi_change_pct + price_change) * 15)
                    regime = "capitulation"

            if abs(strength) < 0.05:
                return

            # Confidence based on OI magnitude and trend consistency
            confidence = min(1.0, abs(oi_trend) * 10 + 0.3)

            sig = Signal(
                self.name, self.asset,
                "long" if strength > 0 else "short",
                strength, confidence,
                f"{regime} oi_chg={oi_change_pct:+.3%} price_chg={price_change:+.3%} oi={oi:.0f}",
            )
            await self.bus.publish("signal.open_interest", sig)
            return  # found our symbol
