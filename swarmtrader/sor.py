"""Smart Order Router — multi-venue quote aggregation and best-execution routing.

Simulates quotes from multiple exchanges (Kraken, Binance, Coinbase, OKX, dYdX)
with small random spread offsets for hackathon demo. In production, each venue
adapter would hit real order-book APIs.

Execution flow integration:
    market.snapshot -> sor quote cache update
    exec.go        -> sor intercepts, picks best venue, publishes sor.routed
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

from .core import Bus, TradeIntent, MarketSnapshot, QUOTE_ASSETS

log = logging.getLogger("swarm.sor")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class ExchangeQuote:
    exchange: str
    pair: str
    bid: float
    ask: float
    volume_24h: float
    fee_rate: float
    latency_ms: float
    ts: float


@dataclass
class VenueConfig:
    name: str
    base_url: str
    fee_rate: float
    enabled: bool = True
    weight: float = 1.0  # priority weight (higher = preferred)


# ---------------------------------------------------------------------------
# Default venue configurations (simulated for hackathon)
# ---------------------------------------------------------------------------
DEFAULT_VENUES: list[VenueConfig] = [
    VenueConfig("kraken",   "https://api.kraken.com",     fee_rate=0.0026, weight=1.0),
    VenueConfig("binance",  "https://api.binance.com",    fee_rate=0.0010, weight=0.9),
    VenueConfig("coinbase", "https://api.coinbase.com",   fee_rate=0.0040, weight=0.7),
    VenueConfig("okx",      "https://www.okx.com",        fee_rate=0.0008, weight=0.85),
    VenueConfig("dydx",     "https://api.dydx.exchange",  fee_rate=0.0005, weight=0.8),
]

# dYdX only supports perpetual pairs
_DYDX_PAIRS = {"BTC", "ETH", "SOL", "AVAX", "DOGE", "LINK", "MATIC"}


# ---------------------------------------------------------------------------
# SmartOrderRouter
# ---------------------------------------------------------------------------
class SmartOrderRouter:
    """Aggregates quotes from multiple venues and routes to best execution."""

    def __init__(self, bus: Bus, venues: list[VenueConfig] | None = None):
        self.bus = bus
        self.venues = venues or list(DEFAULT_VENUES)
        self._kraken_prices: dict[str, float] = {}
        self._quote_cache: dict[str, list[ExchangeQuote]] = {}  # asset -> quotes
        self._routing_log: list[dict[str, Any]] = []
        self._total_savings_bps: float = 0.0
        self._route_count: int = 0

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("exec.go", self._on_exec_go)

    # ── Bus handlers ──────────────────────────────────────────────

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        self._kraken_prices.update(snap.prices)
        # Refresh simulated quotes for all known assets
        for asset in snap.prices:
            self._quote_cache[asset] = self._simulate_quotes(asset, snap.prices[asset])

    async def _on_exec_go(self, intent: TradeIntent) -> None:
        """Intercept execution-go events to route to best venue.

        Publishes sor.routed with the selected venue and adjusted price,
        which the executor can use for the actual fill.
        """
        try:
            venue_name, quote = await self.route(intent)
            log.info("SOR routed %s -> %s (ask=%.2f bid=%.2f fee=%.4f)",
                     intent.id, venue_name, quote.ask, quote.bid, quote.fee_rate)
        except Exception:
            log.debug("SOR: no route found for %s, falling through to default", intent.id)

    # ── Quote simulation ──────────────────────────────────────────

    def _simulate_quotes(self, asset: str, kraken_mid: float) -> list[ExchangeQuote]:
        """Generate simulated quotes for all enabled venues based on Kraken mid."""
        quotes: list[ExchangeQuote] = []
        now = time.time()
        for v in self.venues:
            if not v.enabled:
                continue
            # dYdX only quotes perps for supported assets
            if v.name == "dydx" and asset.upper() not in _DYDX_PAIRS:
                continue
            # Random spread offset: -15 to +15 bps from Kraken mid
            spread_bps = random.uniform(-15, 15) / 10_000
            mid = kraken_mid * (1 + spread_bps)
            # Half-spread: 2-8 bps
            half_spread = random.uniform(2, 8) / 10_000
            bid = mid * (1 - half_spread)
            ask = mid * (1 + half_spread)
            if bid >= ask:
                bid, ask = ask, bid
            # Simulated volume proportional to venue weight
            vol_24h = random.uniform(500_000, 5_000_000) * v.weight
            latency = random.uniform(5, 80) if v.name != "kraken" else random.uniform(2, 20)

            quotes.append(ExchangeQuote(
                exchange=v.name,
                pair=f"{asset}/USD",
                bid=bid,
                ask=ask,
                volume_24h=vol_24h,
                fee_rate=v.fee_rate,
                latency_ms=latency,
                ts=now,
            ))
        return quotes

    # ── Public API ────────────────────────────────────────────────

    async def get_quotes(self, asset: str, side: str = "buy") -> list[ExchangeQuote]:
        """Fetch all available quotes for an asset. Returns cached or freshly
        simulated quotes sorted by effective price."""
        asset_key = asset.upper()
        if asset_key in self._quote_cache:
            quotes = list(self._quote_cache[asset_key])
        elif asset_key in self._kraken_prices:
            quotes = self._simulate_quotes(asset_key, self._kraken_prices[asset_key])
            self._quote_cache[asset_key] = quotes
        else:
            return []

        # Sort by effective price (best for the trader)
        if side == "buy":
            quotes.sort(key=lambda q: q.ask * (1 + q.fee_rate))
        else:
            quotes.sort(key=lambda q: -(q.bid * (1 - q.fee_rate)))
        return quotes

    async def route(self, intent: TradeIntent) -> tuple[str, ExchangeQuote]:
        """Pick the best venue for a TradeIntent by effective price including fees.

        Returns (venue_name, best_quote).
        Publishes ``sor.routed`` with routing details.
        """
        # Determine asset and side
        if intent.asset_in.upper() in QUOTE_ASSETS:
            asset = intent.asset_out
            side = "buy"
        else:
            asset = intent.asset_in
            side = "sell"

        quotes = await self.get_quotes(asset, side)
        if not quotes:
            raise ValueError(f"No quotes available for {asset}")

        best = quotes[0]
        # Calculate savings vs worst quote
        worst = quotes[-1]
        if side == "buy":
            best_eff = best.ask * (1 + best.fee_rate)
            worst_eff = worst.ask * (1 + worst.fee_rate)
            savings_bps = (worst_eff - best_eff) / worst_eff * 10_000
        else:
            best_eff = best.bid * (1 - best.fee_rate)
            worst_eff = worst.bid * (1 - worst.fee_rate)
            savings_bps = (best_eff - worst_eff) / best_eff * 10_000

        savings_bps = max(0.0, savings_bps)

        # Record routing decision
        entry = {
            "ts": time.time(),
            "intent_id": intent.id,
            "asset": asset,
            "side": side,
            "venue": best.exchange,
            "effective_price": best_eff,
            "savings_bps": round(savings_bps, 2),
            "venues_quoted": len(quotes),
        }
        self._routing_log.append(entry)
        self._total_savings_bps += savings_bps
        self._route_count += 1

        await self.bus.publish("sor.routed", {
            "intent_id": intent.id,
            "venue": best.exchange,
            "quote": {
                "exchange": best.exchange,
                "pair": best.pair,
                "bid": best.bid,
                "ask": best.ask,
                "fee_rate": best.fee_rate,
            },
            "savings_bps": round(savings_bps, 2),
        })

        return best.exchange, best

    def best_execution_report(self) -> dict:
        """Return aggregate statistics on routing decisions and savings."""
        if not self._routing_log:
            return {
                "total_routes": 0,
                "avg_savings_bps": 0.0,
                "venue_distribution": {},
                "recent_routes": [],
            }

        venue_counts: dict[str, int] = {}
        for entry in self._routing_log:
            v = entry["venue"]
            venue_counts[v] = venue_counts.get(v, 0) + 1

        return {
            "total_routes": self._route_count,
            "avg_savings_bps": round(self._total_savings_bps / max(1, self._route_count), 2),
            "venue_distribution": venue_counts,
            "recent_routes": self._routing_log[-10:],
        }

    async def run(self) -> None:
        """Periodic quote refresh loop. Run as a background task."""
        log.info("SOR started with %d venues", len([v for v in self.venues if v.enabled]))
        while True:
            await asyncio.sleep(5.0)
            # Refresh quotes for all cached assets
            for asset, price in list(self._kraken_prices.items()):
                self._quote_cache[asset] = self._simulate_quotes(asset, price)


# ---------------------------------------------------------------------------
# Risk check: minimum venue availability
# ---------------------------------------------------------------------------
def sor_venue_check(router: SmartOrderRouter, min_venues: int = 2):
    """Risk check function: rejects if fewer than min_venues are quoting.

    Returns a callable ``(intent) -> (ok, reason)`` for use with RiskAgent.
    """
    def check(intent: TradeIntent) -> tuple[bool, str]:
        if intent.asset_in.upper() in QUOTE_ASSETS:
            asset = intent.asset_out
        else:
            asset = intent.asset_in

        quotes = router._quote_cache.get(asset.upper(), [])
        n = len(quotes)
        if n < min_venues:
            return False, f"sor_venue: only {n}/{min_venues} venues quoting {asset}"
        return True, f"sor_venue: {n} venues quoting {asset}"

    return check
