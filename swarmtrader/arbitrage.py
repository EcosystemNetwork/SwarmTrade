"""Cross-exchange arbitrage detector: monitors price discrepancies between
exchanges to identify market inefficiencies and directional pressure.

Even when not executing arb trades directly, cross-exchange spreads reveal:
  - Which exchange is leading price discovery
  - Directional pressure from geographic/regulatory differences
  - Liquidity imbalances that precede large moves

Uses free public APIs from CoinGecko (aggregated ticker data).
"""
from __future__ import annotations
import asyncio, logging, time
from collections import deque
import aiohttp
from .core import Bus, Signal

log = logging.getLogger("swarm.arb")

COINGECKO_BASE = "https://api.coingecko.com/api/v3"

ASSET_TO_COINGECKO = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "XRP": "ripple", "ADA": "cardano", "DOT": "polkadot",
    "LINK": "chainlink", "AVAX": "avalanche-2",
}

# Major exchanges to track for price comparison
TARGET_EXCHANGES = {
    "binance", "coinbase", "kraken", "okx", "bybit",
    "bitfinex", "kucoin", "huobi", "gate_io",
}


class ArbitrageAgent:
    """Monitors cross-exchange price discrepancies for arbitrage signals
    and market structure intelligence.

    Signals:
      - Large spread between exchanges → market stress / directional pressure
      - One exchange consistently leading → follow the leader
      - Spread contracting after expansion → mean-reversion opportunity

    Publishes signal.arbitrage per asset.
    """

    name = "arbitrage"

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 120.0):
        self.bus = bus
        self.assets = assets or ["ETH", "BTC"]
        self.interval = interval
        self._stop = False

        # Track spread history
        self._spread_history: dict[str, deque[float]] = {
            a: deque(maxlen=30) for a in self.assets
        }
        # Track which exchange leads
        self._leader_history: dict[str, deque[str]] = {
            a: deque(maxlen=20) for a in self.assets
        }

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("ArbitrageAgent starting: assets=%s", self.assets)
        async with aiohttp.ClientSession() as session:
            while not self._stop:
                for asset in self.assets:
                    await self._check_spreads(session, asset)
                await asyncio.sleep(self.interval)

    async def _check_spreads(self, session: aiohttp.ClientSession, asset: str):
        cg_id = ASSET_TO_COINGECKO.get(asset.upper())
        if not cg_id:
            return

        tickers = await self._fetch_tickers(session, cg_id)
        if not tickers:
            return

        # Filter to USD/USDT pairs on major exchanges
        usd_prices: dict[str, float] = {}
        for ticker in tickers:
            exchange = ticker.get("market", {}).get("identifier", "")
            target = ticker.get("target", "").upper()
            if exchange not in TARGET_EXCHANGES:
                continue
            if target not in ("USD", "USDT", "USDC", "BUSD"):
                continue

            price = ticker.get("last", 0)
            volume = ticker.get("converted_volume", {}).get("usd", 0)

            if price > 0 and volume > 10000:  # min $10k volume
                usd_prices[exchange] = price

        if len(usd_prices) < 2:
            return

        prices = list(usd_prices.values())
        exchanges = list(usd_prices.keys())

        max_price = max(prices)
        min_price = min(prices)
        mid_price = sum(prices) / len(prices)

        max_exchange = exchanges[prices.index(max_price)]
        min_exchange = exchanges[prices.index(min_price)]

        # Spread as percentage
        spread_pct = (max_price - min_price) / mid_price if mid_price > 0 else 0
        self._spread_history[asset].append(spread_pct)

        # Track price leader (highest price exchange)
        self._leader_history[asset].append(max_exchange)

        strength = 0.0
        rationale_parts = [f"spread={spread_pct:.4%}"]
        rationale_parts.append(f"high={max_exchange}:{max_price:.2f}")
        rationale_parts.append(f"low={min_exchange}:{min_price:.2f}")

        # --- Signal 1: Large spread = market stress ---
        if spread_pct > 0.005:  # >0.5% spread is unusual
            # Direction: follow the leader (highest price exchange)
            # If premium is on a specific exchange, price likely moves there
            strength = min(0.5, (spread_pct - 0.003) * 50)
            rationale_parts.append("WIDE_SPREAD")

        # --- Signal 2: Spread trend ---
        if len(self._spread_history[asset]) >= 5:
            hist = list(self._spread_history[asset])
            recent_avg = sum(hist[-3:]) / 3
            older_avg = sum(hist[:3]) / 3

            if recent_avg > older_avg * 1.5 and recent_avg > 0.003:
                # Spread widening = increasing stress/opportunity
                strength *= 1.3
                rationale_parts.append("spread_widening")
            elif recent_avg < older_avg * 0.5 and older_avg > 0.003:
                # Spread contracting = normalizing
                strength *= 0.5
                rationale_parts.append("spread_contracting")

        # --- Signal 3: Consistent leader ---
        if len(self._leader_history[asset]) >= 5:
            leaders = list(self._leader_history[asset])
            last_5 = leaders[-5:]
            most_common = max(set(last_5), key=last_5.count)
            frequency = last_5.count(most_common) / 5

            if frequency >= 0.8:
                # One exchange consistently has the highest price
                # This exchange is leading price discovery
                rationale_parts.append(f"leader={most_common}({frequency:.0%})")
                # Slight directional bias: if premium persists, price trends up
                if spread_pct > 0.002:
                    strength += 0.1

        # --- Signal 4: Kraken-specific premium/discount ---
        kraken_price = usd_prices.get("kraken")
        if kraken_price and mid_price > 0:
            kraken_premium = (kraken_price - mid_price) / mid_price
            if abs(kraken_premium) > 0.002:
                # We trade on Kraken, so Kraken premium matters directly
                rationale_parts.append(f"kraken_prem={kraken_premium:+.3%}")
                if kraken_premium > 0.003:
                    # Kraken premium = buyers aggressive on our exchange
                    strength += min(0.2, kraken_premium * 30)
                elif kraken_premium < -0.003:
                    # Kraken discount = sellers aggressive on our exchange
                    strength -= min(0.2, abs(kraken_premium) * 30)

        strength = max(-1.0, min(1.0, strength))
        if abs(strength) < 0.03:
            return

        confidence = min(1.0, 0.3 + len(usd_prices) * 0.1)

        sig = Signal(
            self.name, asset,
            "long" if strength > 0 else "short",
            strength, confidence,
            " ".join(rationale_parts),
        )
        await self.bus.publish("signal.arbitrage", sig)

    async def _fetch_tickers(self, session: aiohttp.ClientSession,
                              cg_id: str) -> list[dict]:
        url = f"{COINGECKO_BASE}/coins/{cg_id}/tickers"
        params = {"include_exchange_logo": "false", "depth": "true"}
        try:
            async with session.get(url, params=params,
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    log.debug("CoinGecko tickers %s returned %d", cg_id, resp.status)
                    return []
                data = await resp.json()
                return data.get("tickers", [])
        except Exception as e:
            log.debug("CoinGecko tickers failed for %s: %s", cg_id, e)
            return []
