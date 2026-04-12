"""Fear & Greed Index agent: polls alternative.me's Crypto Fear & Greed Index
and produces contrarian trading signals.

The index ranges 0-100:
  0-24   = Extreme Fear   → contrarian buy signal
  25-49  = Fear           → mild buy signal
  50-74  = Greed          → mild sell signal
  75-100 = Extreme Greed  → contrarian sell signal

No API key required — free public endpoint.
"""
from __future__ import annotations
import asyncio, logging, time
from collections import deque
import aiohttp
from .core import Bus, Signal

log = logging.getLogger("swarm.feargreed")

FEAR_GREED_URL = "https://api.alternative.me/fng/"


class FearGreedAgent:
    """Fetches the Crypto Fear & Greed Index and converts it to a
    contrarian trading signal.

    Extreme sentiment is historically a reliable contrarian indicator:
    markets tend to reverse when sentiment hits extremes.

    Publishes signal.fear_greed (applied to all tracked assets).
    """

    name = "fear_greed"

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 300.0, window: int = 7):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval  # 5min default (index updates daily)
        self.history: deque[int] = deque(maxlen=window)
        self._stop = False

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("FearGreedAgent starting: interval=%.0fs", self.interval)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                await self._poll(session)
                await asyncio.sleep(self.interval)

    async def _poll(self, session: aiohttp.ClientSession):
        try:
            # Fetch current + historical data
            params = {"limit": "7", "format": "json"}
            async with session.get(FEAR_GREED_URL, params=params,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    log.debug("Fear & Greed API returned %d", resp.status)
                    return
                data = await resp.json()
        except Exception as e:
            log.debug("Fear & Greed fetch failed: %s", e)
            return

        entries = data.get("data", [])
        if not entries:
            return

        # Current value
        current = int(entries[0].get("value", 50))
        classification = entries[0].get("value_classification", "")

        # Update history
        self.history.clear()
        for entry in reversed(entries):  # oldest first
            self.history.append(int(entry.get("value", 50)))

        # Compute trend: is fear/greed getting more extreme?
        if len(self.history) >= 3:
            recent_avg = sum(list(self.history)[-3:]) / 3
            older_avg = sum(list(self.history)[:3]) / 3
            trend = recent_avg - older_avg  # positive = moving toward greed
        else:
            trend = 0.0

        # Contrarian signal: extreme fear = bullish, extreme greed = bearish
        # Map 0-100 to contrarian strength
        if current <= 20:
            # Extreme fear → strong buy
            strength = 0.6 + (20 - current) / 20 * 0.4  # 0.6 to 1.0
        elif current <= 35:
            # Fear → mild buy
            strength = 0.2 + (35 - current) / 15 * 0.4  # 0.2 to 0.6
        elif current <= 65:
            # Neutral zone → weak/no signal
            strength = (50 - current) / 50 * 0.2  # -0.2 to 0.2
        elif current <= 80:
            # Greed → mild sell
            strength = -0.2 - (current - 65) / 15 * 0.4  # -0.2 to -0.6
        else:
            # Extreme greed → strong sell
            strength = -0.6 - (current - 80) / 20 * 0.4  # -0.6 to -1.0

        strength = max(-1.0, min(1.0, strength))

        # Trend amplification: if fear is deepening or greed is increasing
        if trend * strength > 0:
            # Trend is pushing sentiment further into extreme
            strength *= min(1.5, 1.0 + abs(trend) / 30)
            strength = max(-1.0, min(1.0, strength))

        if abs(strength) < 0.05:
            return

        # Confidence based on how extreme the reading is
        distance_from_neutral = abs(current - 50)
        confidence = min(1.0, distance_from_neutral / 40 * 0.7 + 0.3)

        rationale = (
            f"fgi={current} ({classification}) "
            f"trend={trend:+.1f} "
            f"hist=[{','.join(str(v) for v in self.history)}]"
        )

        # Apply to all tracked assets (market-wide sentiment)
        for asset in self.assets:
            sig = Signal(
                self.name, asset,
                "long" if strength > 0 else "short",
                strength, confidence,
                rationale,
            )
            await self.bus.publish("signal.fear_greed", sig)
