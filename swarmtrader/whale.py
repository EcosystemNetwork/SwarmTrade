"""Whale alert agent: monitors large transactions on-chain via public APIs
and converts them into trading signals."""
from __future__ import annotations
import asyncio, logging, os, time
import aiohttp
from .core import Bus, Signal

log = logging.getLogger("swarm.whale")

_last_api_call: dict[str, float] = {}
_MIN_API_INTERVAL = 2.0  # minimum seconds between API calls


async def _rate_limit(api_name: str):
    now = time.time()
    last = _last_api_call.get(api_name, 0.0)
    wait = _MIN_API_INTERVAL - (now - last)
    if wait > 0:
        await asyncio.sleep(wait)
    _last_api_call[api_name] = time.time()

# Blockchain.com unconfirmed tx endpoint (no auth needed)
BLOCKCHAIN_UNCONFIRMED = "https://blockchain.info/unconfirmed-transactions?format=json"

# Blockchair API (free tier, 30 req/min)
BLOCKCHAIR_BASE = "https://api.blockchair.com"


class WhaleAgent:
    """Tracks unusually large on-chain transactions and exchange flows.

    Large inflows to exchanges → bearish (likely selling).
    Large outflows from exchanges → bullish (likely accumulating).
    Large whale-to-whale transfers → neutral but increases volatility expectation.

    Uses free public blockchain APIs (no key required for basic tier).

    Publishes signal.whale per asset.
    """

    name = "whale"

    # Known exchange deposit addresses (subset — extend as needed)
    EXCHANGE_ADDRESSES = {
        # Kraken BTC hot wallets (publicly known)
        "3AfSvhERtFHQ1WE6miFqvBBfKBB1TtNZFp",
        "bc1qmxjefnuy06v345v6vhwpwt05dztztmx4g3y7wp",
        # Binance
        "34xp4vRoCGJym3xR7yCVPFHoCNxv4Twseo",
        "bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h",
        # Coinbase
        "3Kzh9qAqVWQhEsfQz7zEQL1EuSx5tyNLNS",
        "bc1q7cyrfmck2ffu2ud3rn5l5a8yv6f0chkp0zpemf",
    }

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 120.0, min_btc: float = 100.0,
                 min_eth: float = 1000.0):
        self.bus = bus
        self.assets = assets or ["BTC", "ETH"]
        self.interval = interval
        self.min_btc = min_btc  # minimum BTC value to flag
        self.min_eth = min_eth  # minimum ETH value to flag
        self._stop = False
        self._seen_tx: set[str] = set()
        self._seen_max = 500

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("WhaleAgent starting: assets=%s interval=%.0fs", self.assets, self.interval)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                if "BTC" in self.assets:
                    await self._check_btc_whales(session)
                if "ETH" in self.assets:
                    await self._check_eth_whales(session)
                await asyncio.sleep(self.interval)

    async def _check_btc_whales(self, session: aiohttp.ClientSession):
        try:
            url = f"{BLOCKCHAIR_BASE}/bitcoin/transactions"
            params = {"s": "output_total(desc)", "limit": 10}
            await _rate_limit("blockchair_btc")
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.debug("Blockchair BTC returned %d", resp.status)
                    return
                data = await resp.json()

            txs = data.get("data", [])
            whale_signals: list[tuple[float, str]] = []

            for tx in txs:
                tx_hash = tx.get("hash", "")
                if tx_hash in self._seen_tx:
                    continue

                output_btc = tx.get("output_total", 0) / 1e8  # satoshis → BTC
                if output_btc < self.min_btc:
                    continue

                self._seen_tx.add(tx_hash)
                if len(self._seen_tx) > self._seen_max:
                    # Trim oldest
                    self._seen_tx = set(list(self._seen_tx)[-self._seen_max // 2:])

                # Check if outputs go to known exchange addresses
                output_addrs = set(tx.get("output_addresses", []) or [])
                input_addrs = set(tx.get("input_addresses", []) or [])

                to_exchange = bool(output_addrs & self.EXCHANGE_ADDRESSES)
                from_exchange = bool(input_addrs & self.EXCHANGE_ADDRESSES)

                if to_exchange:
                    # Inflow to exchange = likely selling = bearish
                    whale_signals.append((-output_btc, f"→exchange {output_btc:.1f}BTC"))
                elif from_exchange:
                    # Outflow from exchange = likely accumulating = bullish
                    whale_signals.append((output_btc, f"exchange→ {output_btc:.1f}BTC"))
                else:
                    # Whale-to-whale: neutral but vol signal
                    whale_signals.append((0, f"whale↔whale {output_btc:.1f}BTC"))

            if not whale_signals:
                return

            # Aggregate signals
            directional = [s for s, _ in whale_signals if s != 0]
            total_btc = sum(abs(s) for s, _ in whale_signals)

            if directional:
                net = sum(directional)
                strength = max(-1.0, min(1.0, net / (self.min_btc * 5)))
            else:
                strength = 0.0

            confidence = min(1.0, total_btc / (self.min_btc * 10))
            rationales = [r for _, r in whale_signals[:3]]

            if abs(strength) < 0.03 and not whale_signals:
                return

            sig = Signal(
                self.name, "BTC",
                "long" if strength > 0 else ("short" if strength < 0 else "flat"),
                strength, confidence,
                f"whales: {'; '.join(rationales)}",
            )
            await self.bus.publish("signal.whale", sig)

        except Exception as e:
            log.debug("WhaleAgent BTC error: %s", e)

    async def _check_eth_whales(self, session: aiohttp.ClientSession):
        try:
            url = f"{BLOCKCHAIR_BASE}/ethereum/transactions"
            params = {"s": "value(desc)", "limit": 10}
            await _rate_limit("blockchair_eth")
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.debug("Blockchair ETH returned %d", resp.status)
                    return
                data = await resp.json()

            txs = data.get("data", [])
            whale_count = 0
            net_direction = 0.0
            rationales: list[str] = []

            for tx in txs:
                tx_hash = tx.get("hash", "")
                if tx_hash in self._seen_tx:
                    continue

                value_eth = tx.get("value", 0) / 1e18  # wei → ETH
                if value_eth < self.min_eth:
                    continue

                self._seen_tx.add(tx_hash)
                whale_count += 1
                rationales.append(f"{value_eth:.0f}ETH")

                # For ETH we don't have easy exchange address detection
                # in the free API, so we track volume only
                net_direction += value_eth

            if whale_count == 0:
                return

            # Volume-only signal: large whale activity = expect volatility
            strength = 0.0  # directionally neutral without exchange detection
            confidence = min(1.0, whale_count / 5.0)

            sig = Signal(
                self.name, "ETH", "flat",
                strength, confidence,
                f"whale_vol: {whale_count}tx {'; '.join(rationales[:3])}",
            )
            await self.bus.publish("signal.whale", sig)

        except Exception as e:
            log.debug("WhaleAgent ETH error: %s", e)
