"""On-chain activity agent: monitors blockchain metrics as leading indicators.

Active addresses rising     → adoption/usage growing → bullish
Gas prices spiking          → congestion/demand → short-term bearish (costs), long-term bullish (usage)
DEX volume surging          → retail activity spike → potential volatility
Exchange reserves declining → accumulation → bullish

Uses free APIs: Blockchair, Etherscan (optional key for higher rate limits).
"""
from __future__ import annotations
import asyncio, logging, os, time
from collections import deque
import aiohttp
from .core import Bus, Signal

log = logging.getLogger("swarm.onchain")

BLOCKCHAIR_BASE = "https://api.blockchair.com"
ETHERSCAN_BASE = "https://api.etherscan.io/api"


class OnChainAgent:
    """Monitors on-chain metrics for ETH and BTC as fundamental signals.

    Metrics tracked:
      - Transaction count (network activity)
      - Average transaction value (whale vs retail)
      - Gas price / fee levels (demand for block space)

    Publishes signal.onchain per asset.
    """

    name = "onchain"

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 300.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False
        self.etherscan_key = os.getenv("ETHERSCAN_API_KEY", "")

        # History for trend detection
        self._tx_history: dict[str, deque[float]] = {
            a: deque(maxlen=24) for a in self.assets
        }
        self._gas_history: deque[float] = deque(maxlen=24)

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("OnChainAgent starting: assets=%s", self.assets)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                for asset in self.assets:
                    await self._analyze(session, asset)
                await asyncio.sleep(self.interval)

    async def _analyze(self, session: aiohttp.ClientSession, asset: str):
        strength = 0.0
        confidence = 0.0
        rationale_parts = []

        if asset == "ETH":
            stats = await self._fetch_eth_stats(session)
            gas = await self._fetch_gas_price(session)

            if stats:
                tx_count = stats.get("transactions_24h", 0)
                if tx_count > 0:
                    self._tx_history[asset].append(tx_count)

                    if len(self._tx_history[asset]) >= 3:
                        hist = list(self._tx_history[asset])
                        avg = sum(hist[:-1]) / len(hist[:-1])
                        if avg > 0:
                            ratio = tx_count / avg
                            if ratio > 1.1:
                                strength += min(0.3, (ratio - 1) * 1.5)
                                rationale_parts.append(f"tx_surge={ratio:.2f}x")
                            elif ratio < 0.85:
                                strength -= min(0.2, (1 - ratio) * 1.0)
                                rationale_parts.append(f"tx_decline={ratio:.2f}x")

                avg_value = stats.get("average_transaction_value_usd", 0)
                if avg_value > 5000:
                    strength += 0.1  # large average tx = institutional activity
                    rationale_parts.append(f"avg_tx=${avg_value:.0f}")

            if gas and isinstance(gas, (int, float)):
                self._gas_history.append(gas)
                if len(self._gas_history) >= 3:
                    hist = list(self._gas_history)
                    avg_gas = sum(hist[:-1]) / len(hist[:-1])
                    if avg_gas > 0:
                        gas_ratio = gas / avg_gas
                        if gas_ratio > 2.0:
                            # Gas spike = high demand, short-term bearish
                            strength -= min(0.2, (gas_ratio - 1) * 0.1)
                            rationale_parts.append(f"gas_spike={gas:.0f}gwei ({gas_ratio:.1f}x)")
                        elif gas_ratio < 0.5:
                            # Very low gas = quiet network
                            rationale_parts.append(f"gas_low={gas:.0f}gwei")

        elif asset == "BTC":
            stats = await self._fetch_btc_stats(session)
            if stats:
                tx_count = stats.get("transactions_24h", 0)
                if tx_count > 0:
                    self._tx_history[asset].append(tx_count)

                    if len(self._tx_history[asset]) >= 3:
                        hist = list(self._tx_history[asset])
                        avg = sum(hist[:-1]) / len(hist[:-1])
                        if avg > 0:
                            ratio = tx_count / avg
                            if ratio > 1.1:
                                strength += min(0.3, (ratio - 1) * 1.5)
                                rationale_parts.append(f"tx_surge={ratio:.2f}x")
                            elif ratio < 0.85:
                                strength -= min(0.2, (1 - ratio) * 1.0)
                                rationale_parts.append(f"tx_decline={ratio:.2f}x")

                hashrate = stats.get("hashrate_24h", 0)
                if hashrate:
                    rationale_parts.append(f"hashrate={_human_number(hashrate)}")
                    # Rising hashrate = miner confidence = bullish
                    strength += 0.05

        strength = max(-1.0, min(1.0, strength))
        if abs(strength) < 0.03:
            return

        data_points = len(rationale_parts)
        confidence = min(1.0, 0.3 + data_points * 0.15)

        sig = Signal(
            self.name, asset,
            "long" if strength > 0 else "short",
            strength, confidence,
            " ".join(rationale_parts) or "onchain_neutral",
        )
        await self.bus.publish("signal.onchain", sig)

    async def _fetch_eth_stats(self, session: aiohttp.ClientSession) -> dict:
        try:
            url = f"{BLOCKCHAIR_BASE}/ethereum/stats"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                return data.get("data", {})
        except Exception as e:
            log.debug("ETH stats fetch failed: %s", e)
            return {}

    async def _fetch_btc_stats(self, session: aiohttp.ClientSession) -> dict:
        try:
            url = f"{BLOCKCHAIR_BASE}/bitcoin/stats"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    return {}
                data = await resp.json()
                return data.get("data", {})
        except Exception as e:
            log.debug("BTC stats fetch failed: %s", e)
            return {}

    async def _fetch_gas_price(self, session: aiohttp.ClientSession) -> float | None:
        """Fetch current ETH gas price in gwei."""
        if self.etherscan_key:
            try:
                params = {
                    "module": "gastracker",
                    "action": "gasoracle",
                    "apikey": self.etherscan_key,
                }
                async with session.get(ETHERSCAN_BASE, params=params,
                                       timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    result = data.get("result", {})
                    return float(result.get("ProposeGasPrice", 0))
            except Exception:
                pass

        # Fallback: Blockchair
        try:
            url = f"{BLOCKCHAIR_BASE}/ethereum/stats"
            async with session.get(url,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                stats = data.get("data", {})
                # Blockchair gives suggested gas in wei
                gas_wei = stats.get("suggested_transaction_fee_gwei_options", {})
                if isinstance(gas_wei, dict):
                    return float(gas_wei.get("standard", 0))
                return None
        except Exception:
            return None


def _human_number(n: float) -> str:
    """Format large numbers for readability."""
    for suffix in ("", "K", "M", "B", "T"):
        if abs(n) < 1000:
            return f"{n:.1f}{suffix}"
        n /= 1000
    return f"{n:.1f}P"
