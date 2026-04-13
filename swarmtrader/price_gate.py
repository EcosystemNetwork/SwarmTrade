"""Price Validation Gate — multi-source price verification before execution.

Inspired by TWAP CHOP (ETHGlobal Cannes 2026). Before every trade, validates
price consistency across independent sources. Three-tier response:

  SAFE (<0.5% deviation) — proceed with execution
  WARN (0.5%-2%) — retry after delay, log warning
  HALT (>2%) — abort the trade entirely

Also implements binary search for optimal chunk sizing: finds the largest
order size where estimated price impact stays below a configurable threshold.

Bus integration:
  Subscribes to: exec.go (intercepts before Simulator)
  Publishes to:  exec.validated (passes to Simulator if SAFE)
                 exec.report (rejects if HALT)

Environment variables:
  PRICE_GATE_SAFE_BPS     — max deviation for SAFE tier (default: 50 = 0.5%)
  PRICE_GATE_WARN_BPS     — max deviation for WARN tier (default: 200 = 2%)
  PRICE_GATE_RETRY_DELAY  — seconds to wait on WARN before retry (default: 5)
  PRICE_GATE_MAX_RETRIES  — max retries on WARN (default: 3)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from .core import Bus, TradeIntent, MarketSnapshot, ExecutionReport

log = logging.getLogger("swarm.price_gate")

# Thresholds in basis points
SAFE_BPS = int(os.getenv("PRICE_GATE_SAFE_BPS", "50"))
WARN_BPS = int(os.getenv("PRICE_GATE_WARN_BPS", "200"))
RETRY_DELAY = float(os.getenv("PRICE_GATE_RETRY_DELAY", "5"))
MAX_RETRIES = int(os.getenv("PRICE_GATE_MAX_RETRIES", "3"))


@dataclass
class PriceCheck:
    """Result of a multi-source price validation."""
    asset: str
    sources: dict[str, float]    # source_name -> price
    median_price: float
    max_deviation_bps: float
    tier: str                    # "SAFE", "WARN", "HALT"
    ts: float = field(default_factory=time.time)

    @property
    def is_safe(self) -> bool:
        return self.tier == "SAFE"


class PriceValidationGate:
    """Multi-source price verification gate for the execution pipeline.

    Sits between the Strategist (intent.new) and Simulator (exec.go).
    Intercepts trade intents, validates prices across sources, and only
    forwards to execution if prices are consistent.

    Price sources (populated from market snapshots + external feeds):
      - Internal: latest Bus market snapshot
      - Kraken WS: real-time order book mid
      - CoinGecko: REST price (cached 30s)
      - On-chain: DEX pool prices
    """

    def __init__(self, bus: Bus, safe_bps: int = SAFE_BPS,
                 warn_bps: int = WARN_BPS, kill_switch=None):
        self.bus = bus
        self.kill_switch = kill_switch
        self.safe_bps = safe_bps
        self.warn_bps = warn_bps
        self._prices: dict[str, dict[str, tuple[float, float]]] = {}  # asset -> {source: (price, timestamp)}
        self._stale_threshold = float(os.getenv("PRICE_GATE_STALE_SECS", "60"))  # reject prices older than this
        self._retry_counts: dict[str, int] = {}  # intent_id -> retry count
        self._checks: list[PriceCheck] = []
        self._stats = {"safe": 0, "warn": 0, "halt": 0, "total": 0}

        # Subscribe to price sources
        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("market.kraken_ws", self._on_kraken_price)
        bus.subscribe("market.coingecko", self._on_coingecko_price)
        bus.subscribe("market.dex_quote", self._on_dex_price)

        # Intercept execution flow (after MEV screening)
        bus.subscribe("exec.cleared", self._on_exec_go)

    async def _on_snapshot(self, snap: MarketSnapshot):
        """Update internal price source from market snapshots."""
        now = time.time()
        for asset, price in snap.prices.items():
            self._prices.setdefault(asset, {})["snapshot"] = (price, now)

    async def _on_kraken_price(self, data: dict):
        asset = data.get("asset", "")
        price = data.get("mid", 0.0)
        if asset and price > 0:
            self._prices.setdefault(asset, {})["kraken_ws"] = (price, time.time())

    async def _on_coingecko_price(self, data: dict):
        asset = data.get("asset", "")
        price = data.get("price", 0.0)
        if asset and price > 0:
            self._prices.setdefault(asset, {})["coingecko"] = (price, time.time())

    async def _on_dex_price(self, data: dict):
        asset = data.get("asset", "")
        price = data.get("price", 0.0)
        if asset and price > 0:
            self._prices.setdefault(asset, {})["dex"] = (price, time.time())

    def _extract_asset(self, intent: TradeIntent) -> str:
        """Get the base asset from an intent."""
        from .core import QUOTE_ASSETS
        if intent.asset_in.upper() in QUOTE_ASSETS:
            return intent.asset_out
        return intent.asset_in

    def validate_price(self, asset: str) -> PriceCheck:
        """Check price consistency across all available sources.

        Filters out stale sources (older than _stale_threshold seconds).
        """
        raw_sources = self._prices.get(asset, {})
        now = time.time()
        # Filter stale prices
        sources: dict[str, float] = {}
        for src, entry in raw_sources.items():
            price, ts = entry
            age = now - ts
            if age <= self._stale_threshold:
                sources[src] = price
            else:
                log.debug("PRICE GATE: dropping stale %s source for %s (%.0fs old)", src, asset, age)

        if len(sources) < 1:
            # No price data — cannot validate, HALT to prevent blind execution
            log.warning("PRICE GATE: no price sources for %s — HALT", asset)
            return PriceCheck(
                asset=asset, sources={}, median_price=0.0,
                max_deviation_bps=0.0, tier="HALT",
            )

        if len(sources) < 2:
            # Single source — can't cross-validate, allow with warning
            log.info("PRICE GATE: only 1 source for %s — WARN (cannot cross-validate)", asset)
            price = list(sources.values())[0]
            return PriceCheck(
                asset=asset, sources=dict(sources), median_price=price,
                max_deviation_bps=0.0, tier="WARN",
            )

        prices = list(sources.values())
        prices.sort()

        # Median price
        n = len(prices)
        if n % 2 == 1:
            median = prices[n // 2]
        else:
            median = (prices[n // 2 - 1] + prices[n // 2]) / 2

        if median < 1e-12:
            log.warning("PRICE GATE: near-zero median price for %s — HALT", asset)
            return PriceCheck(
                asset=asset, sources=dict(sources), median_price=0.0,
                max_deviation_bps=0.0, tier="HALT",
            )

        # Max deviation from median in basis points
        max_dev = max(abs(p - median) / median for p in prices) * 10_000

        # Tier classification
        if max_dev <= self.safe_bps:
            tier = "SAFE"
        elif max_dev <= self.warn_bps:
            tier = "WARN"
        else:
            tier = "HALT"

        check = PriceCheck(
            asset=asset, sources=dict(sources), median_price=median,
            max_deviation_bps=round(max_dev, 1), tier=tier,
        )
        self._checks.append(check)
        if len(self._checks) > 500:
            self._checks = self._checks[-250:]

        return check

    async def _on_exec_go(self, intent: TradeIntent):
        """Intercept execution and validate prices first."""
        if self.kill_switch and self.kill_switch.active:
            return  # Kill switch engaged — do not validate or forward
        self._stats["total"] += 1
        asset = self._extract_asset(intent)
        check = self.validate_price(asset)

        if check.tier == "SAFE":
            self._stats["safe"] += 1
            log.debug(
                "PRICE GATE SAFE: %s dev=%.1fbps sources=%d",
                asset, check.max_deviation_bps, len(check.sources),
            )
            await self.bus.publish("exec.validated", intent)

        elif check.tier == "WARN":
            self._stats["warn"] += 1
            retries = self._retry_counts.get(intent.id, 0)
            if retries < MAX_RETRIES:
                self._retry_counts[intent.id] = retries + 1
                log.warning(
                    "PRICE GATE WARN: %s dev=%.1fbps — retry %d/%d in %.0fs",
                    asset, check.max_deviation_bps, retries + 1, MAX_RETRIES, RETRY_DELAY,
                )
                await asyncio.sleep(RETRY_DELAY)
                # Re-validate after delay
                recheck = self.validate_price(asset)
                if recheck.tier == "SAFE":
                    self._stats["safe"] += 1
                    self._retry_counts.pop(intent.id, None)
                    await self.bus.publish("exec.validated", intent)
                elif recheck.tier == "HALT":
                    self._stats["halt"] += 1
                    await self._reject(intent, asset, recheck)
                else:
                    # Still WARN — pass through with warning logged
                    log.warning(
                        "PRICE GATE: %s still WARN after retry (dev=%.1fbps) — proceeding",
                        asset, recheck.max_deviation_bps,
                    )
                    self._retry_counts.pop(intent.id, None)
                    await self.bus.publish("exec.validated", intent)
            else:
                log.warning(
                    "PRICE GATE: %s max retries exhausted — proceeding with caution",
                    asset,
                )
                await self.bus.publish("exec.validated", intent)
                self._retry_counts.pop(intent.id, None)

        else:  # HALT
            self._stats["halt"] += 1
            await self._reject(intent, asset, check)

    async def _reject(self, intent: TradeIntent, asset: str, check: PriceCheck):
        """Reject a trade due to price deviation."""
        source_str = ", ".join(f"{k}=${v:.2f}" for k, v in check.sources.items())
        log.error(
            "PRICE GATE HALT: %s dev=%.1fbps — REJECTING trade %s | sources: %s",
            asset, check.max_deviation_bps, intent.id, source_str,
        )
        rep = ExecutionReport(
            intent_id=intent.id,
            status="rejected",
            tx_hash=None,
            fill_price=None,
            realized_slippage=None,
            pnl_estimate=0.0,
            note=f"price_gate_halt: {asset} deviation={check.max_deviation_bps:.0f}bps ({source_str})",
        )
        await self.bus.publish("exec.report", rep)
        self._retry_counts.pop(intent.id, None)

    def optimal_chunk_size(self, asset: str, total_amount: float,
                           max_impact_bps: float = 50.0,
                           slippage_bps_per_1k: float = 5.0) -> float:
        """Binary search for optimal chunk size given impact constraints.

        Finds the largest single-order size where estimated price impact
        stays below max_impact_bps.

        Args:
            asset: trading asset
            total_amount: total USD to execute
            max_impact_bps: maximum acceptable impact per chunk
            slippage_bps_per_1k: estimated slippage per $1k notional

        Returns:
            Optimal chunk size in USD
        """
        lo, hi = 100.0, total_amount
        # Binary search: find largest chunk where impact <= threshold
        for _ in range(20):  # 20 iterations = precision to ~$0.01
            mid = (lo + hi) / 2
            impact = (mid / 1000.0) * slippage_bps_per_1k
            if impact <= max_impact_bps:
                lo = mid
            else:
                hi = mid
        return round(lo, 2)

    def summary(self) -> dict:
        return {
            **self._stats,
            "safe_rate": self._stats["safe"] / max(self._stats["total"], 1),
            "recent_checks": [
                {
                    "asset": c.asset,
                    "tier": c.tier,
                    "deviation_bps": c.max_deviation_bps,
                    "sources": len(c.sources),
                    "ts": c.ts,
                }
                for c in self._checks[-5:]
            ],
        }
