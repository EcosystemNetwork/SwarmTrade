"""External signal sources: PRISM/Strykr AI signals with full endpoint coverage."""
from __future__ import annotations
import asyncio, logging, os, time
import aiohttp
from .core import Bus, Signal

log = logging.getLogger("swarm.signals")

PRISM_BASE = "https://api.prismapi.ai"


class PRISMSignalAgent:
    """Fetches AI trading signals from PRISM/Strykr API.
    Covers: momentum, volume spikes, breakouts, divergences, risk metrics."""

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 30.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False
        self.api_key = os.getenv("PRISM_API_KEY", "")

    def stop(self):
        self._stop = True

    async def run(self):
        if not self.api_key:
            log.warning("PRISM_API_KEY not set, PRISMSignalAgent disabled")
            return
        log.info("PRISMSignalAgent starting: assets=%s", self.assets)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                for asset in self.assets:
                    await self._fetch_all_signals(session, asset)
                await asyncio.sleep(self.interval)

    async def _fetch_all_signals(self, session: aiohttp.ClientSession, asset: str):
        headers = {"X-API-Key": self.api_key}

        # Fetch all signal types in parallel
        tasks = {
            "signals": self._fetch_json(session, f"{PRISM_BASE}/signals/{asset}", headers),
            "risk": self._fetch_json(session, f"{PRISM_BASE}/risk/{asset}", headers),
            "price": self._fetch_json(session, f"{PRISM_BASE}/crypto/{asset}/price", headers),
        }
        results = {}
        for name, coro in tasks.items():
            try:
                results[name] = await coro
            except Exception as e:
                log.warning("PRISM %s/%s failed: %s", name, asset, e)
                results[name] = {}

        # --- Main signal ---
        signal_data = results.get("signals", {})
        risk_data = results.get("risk", {})
        main_signal = self._parse_main_signal(asset, signal_data, risk_data)
        if main_signal:
            await self.bus.publish("signal.prism", main_signal)

        # --- Volume spike signal ---
        vol_signal = self._parse_volume_signal(asset, signal_data)
        if vol_signal:
            await self.bus.publish("signal.prism_volume", vol_signal)

        # --- Breakout signal ---
        breakout = self._parse_breakout(asset, signal_data)
        if breakout:
            await self.bus.publish("signal.prism_breakout", breakout)

    @staticmethod
    async def _fetch_json(session: aiohttp.ClientSession, url: str,
                          headers: dict) -> dict:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status != 200:
                return {}
            return await resp.json()

    def _parse_main_signal(self, asset: str, data: dict, risk: dict) -> Signal | None:
        sentiment = data.get("sentiment", data.get("signal", data.get("score", 0)))
        if isinstance(sentiment, str):
            sentiment_map = {
                "bullish": 0.6, "bearish": -0.6, "neutral": 0.0,
                "strong_buy": 0.9, "strong_sell": -0.9,
                "buy": 0.5, "sell": -0.5,
            }
            strength = sentiment_map.get(sentiment.lower(), 0.0)
        elif isinstance(sentiment, (int, float)):
            strength = max(-1.0, min(1.0, float(sentiment)))
        else:
            return None

        if abs(strength) < 0.05:
            return None

        volatility = risk.get("volatility", risk.get("vol_24h", 0.5))
        confidence = max(0.2, min(1.0, 1.0 - float(volatility) * 0.5)) if isinstance(volatility, (int, float)) else 0.5

        rationale_parts = [f"prism={strength:+.3f}"]
        if "summary" in data:
            rationale_parts.append(data["summary"][:80])

        return Signal("prism", asset, "long" if strength > 0 else "short",
                      strength, confidence, "; ".join(rationale_parts))

    def _parse_volume_signal(self, asset: str, data: dict) -> Signal | None:
        vol_spike = data.get("volume_spike", data.get("volume_change_pct", None))
        if vol_spike is None:
            return None
        try:
            spike = float(vol_spike)
        except (TypeError, ValueError):
            return None
        if abs(spike) < 10:  # less than 10% volume change
            return None

        # Volume spike is direction-neutral but increases confidence for other signals
        strength = max(-1.0, min(1.0, spike / 200.0))
        confidence = min(1.0, abs(spike) / 100.0)
        return Signal("prism_vol", asset, "long" if strength > 0 else "short",
                      strength, confidence, f"vol_spike={spike:+.1f}%")

    def _parse_breakout(self, asset: str, data: dict) -> Signal | None:
        breakout = data.get("breakout", data.get("breakout_signal", None))
        if breakout is None:
            return None
        if isinstance(breakout, str):
            if "up" in breakout.lower():
                return Signal("prism_breakout", asset, "long", 0.7, 0.6,
                              f"breakout={breakout}")
            elif "down" in breakout.lower():
                return Signal("prism_breakout", asset, "short", -0.7, 0.6,
                              f"breakout={breakout}")
        elif isinstance(breakout, (int, float)) and abs(breakout) > 0.1:
            strength = max(-1.0, min(1.0, float(breakout)))
            return Signal("prism_breakout", asset,
                          "long" if strength > 0 else "short",
                          strength, 0.6, f"breakout={strength:+.3f}")
        return None
