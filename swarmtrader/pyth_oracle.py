"""Pyth Network oracle price feeds — decentralized real-time pricing.

Provides an alternative/redundant price source alongside Kraken.
Uses Pyth's Hermes REST API for price feeds (no on-chain calls needed).

Feed IDs for major assets:
  ETH/USD: 0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace
  BTC/USD: 0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43
  SOL/USD: 0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d
"""
from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from .core import Bus, MarketSnapshot

log = logging.getLogger("swarm.pyth")

_HERMES_URL = "https://hermes.pyth.network"

# Pyth price feed IDs for major crypto assets
PYTH_FEEDS: dict[str, str] = {
    "ETH": "0xff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "BTC": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "SOL": "0xef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "XRP": "0xec5d399846a9209f3fe5881d70aae9268c94339ff9817c800ea6357f21be82b0",
    "ADA": "0x2a01deaec9e51a579277b34b122399984d0bbf57e2458a7e42fecd2829867a0d",
    "DOT": "0xca3eed9ab553d1f8f0d2f5c5c78e51b77e3f4e5dfc4f67c3f69f1f199b6c7ece",
    "LINK": "0x8ac0c70fff57e9aefdf5edf44b51d62c2d433653cbb2cf5cc06bb115af04d221",
    "AVAX": "0x93da3352f9f1d105fdfe4971cfa80e9dd777bfc5d0f683ebb6e1294b92137bb7",
}

# Reverse map for feed ID → symbol
_FEED_TO_SYMBOL = {v: k for k, v in PYTH_FEEDS.items()}


class PythOracle:
    """Fetches decentralized price feeds from Pyth Network's Hermes API.

    Publishes prices to Bus as market.pyth events and optionally
    as market.snapshot for redundancy with Kraken feeds.
    """

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 5.0, publish_snapshot: bool = False):
        self.bus = bus
        self.assets = [a.upper() for a in (assets or ["ETH", "BTC"])]
        self.interval = interval
        self.publish_snapshot = publish_snapshot
        self._stop = False
        self._session: aiohttp.ClientSession | None = None
        self._consecutive_errors = 0

    def stop(self):
        self._stop = True

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def run(self):
        """Poll Pyth Hermes for price feeds."""
        feed_ids = [PYTH_FEEDS[a] for a in self.assets if a in PYTH_FEEDS]
        if not feed_ids:
            log.warning("PythOracle: no valid feed IDs for assets %s", self.assets)
            return

        log.info("PythOracle starting: assets=%s interval=%.1fs", self.assets, self.interval)

        try:
            while not self._stop:
                try:
                    prices = await self._fetch_prices(feed_ids)
                    if prices:
                        # Publish Pyth-specific event
                        await self.bus.publish("market.pyth", {
                            "ts": time.time(),
                            "prices": prices,
                            "source": "pyth_hermes",
                        })

                        # Optionally publish as market.snapshot for redundancy
                        if self.publish_snapshot:
                            snap = MarketSnapshot(
                                ts=time.time(),
                                prices=prices,
                                gas_gwei=0.0,
                            )
                            await self.bus.publish("market.snapshot", snap)

                        if self._consecutive_errors > 0:
                            log.info("PythOracle recovered after %d errors",
                                     self._consecutive_errors)
                        self._consecutive_errors = 0

                    await asyncio.sleep(self.interval)

                except Exception as e:
                    self._consecutive_errors += 1
                    backoff = min(2.0 * (2 ** self._consecutive_errors), 60.0)
                    log.warning("PythOracle error (attempt %d, backoff %.1fs): %s",
                                self._consecutive_errors, backoff, e)
                    await asyncio.sleep(backoff)
        finally:
            await self.close()

    async def _fetch_prices(self, feed_ids: list[str]) -> dict[str, float]:
        """Fetch latest prices from Pyth Hermes API."""
        session = await self._ensure_session()
        params = [("ids[]", fid) for fid in feed_ids]
        url = f"{_HERMES_URL}/v2/updates/price/latest"

        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Pyth API returned {resp.status}")
            data = await resp.json()

        prices: dict[str, float] = {}
        for parsed in data.get("parsed", []):
            feed_id = "0x" + parsed.get("id", "")
            symbol = _FEED_TO_SYMBOL.get(feed_id, "")
            if not symbol:
                continue

            price_data = parsed.get("price", {})
            price_raw = int(price_data.get("price", 0))
            expo = int(price_data.get("expo", 0))

            if price_raw and expo:
                price = price_raw * (10 ** expo)
                if price > 0:
                    prices[symbol] = price

        return prices

    async def get_price(self, asset: str) -> float | None:
        """Fetch a single asset price from Pyth."""
        feed_id = PYTH_FEEDS.get(asset.upper())
        if not feed_id:
            return None
        prices = await self._fetch_prices([feed_id])
        return prices.get(asset.upper())
