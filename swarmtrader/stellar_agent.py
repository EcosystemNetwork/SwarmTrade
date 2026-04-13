"""Stellar Trading Agent — XLM/USDC market data + SDEX execution.

Monitors the Stellar DEX (SDEX) orderbook for XLM/USDC and other Stellar
asset pairs, publishes price data to the swarm bus, and can execute trades
directly on the Stellar decentralized exchange.

Also integrates with the Stellar payment gateway for agent-to-agent
micropayments when buying/selling signals from other agents.

Features:
  - SDEX orderbook monitoring (bids/asks/spread/depth)
  - XLM price feed via Horizon API
  - Path payment optimization (multi-hop swaps)
  - Stellar DEX trade execution (limit + market orders)
  - Agent payment integration (pay-per-signal on Stellar)

Bus topics published:
  stellar.price        — XLM/USDC price updates
  stellar.orderbook    — SDEX orderbook snapshots
  stellar.spread       — bid-ask spread alerts
  stellar.trade        — executed SDEX trades
  stellar.pathfinding  — optimal swap paths

Environment variables:
  STELLAR_SECRET_KEY     — Stellar secret key for trading
  STELLAR_NETWORK        — "testnet" or "public"
  STELLAR_ASSETS         — comma-separated asset codes to track (default: XLM)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field

from .core import Bus, Signal, MarketSnapshot

log = logging.getLogger("swarm.stellar_agent")


@dataclass
class StellarPrice:
    """Price snapshot from the Stellar DEX."""
    asset: str
    price_usd: float
    bid: float
    ask: float
    spread_bps: float
    volume_24h: float
    timestamp: float = field(default_factory=time.time)
    source: str = "sdex"


class StellarDEXAgent:
    """Monitors Stellar DEX and publishes market data to the swarm.

    Runs as a data scout alongside existing CEX scouts (Kraken, etc.),
    providing SDEX-specific intelligence:
    - XLM/USDC price feed from the native Stellar orderbook
    - Spread monitoring for arbitrage opportunities
    - Liquidity depth analysis
    - Path payment routing for optimal execution
    """

    name = "stellar_dex"

    def __init__(
        self,
        bus: Bus,
        assets: list[str] | None = None,
        interval: float = 10.0,
        network: str = "testnet",
    ):
        self.bus = bus
        self._assets = assets or ["XLM"]
        self._interval = interval
        self._network = network
        self._stop = False
        self._server = None
        self._prices: dict[str, StellarPrice] = {}
        self._usdc_issuer = ""

        # Stats
        self._tick_count = 0
        self._errors = 0
        self._last_tick = 0.0

        bus.subscribe("stellar.execute_trade", self._on_trade_request)

    async def run(self):
        """Main loop: poll SDEX orderbook and publish price data."""
        try:
            from stellar_sdk import Server, Asset
            from .stellar_payments import STELLAR_NETWORKS

            net_cfg = STELLAR_NETWORKS.get(self._network, STELLAR_NETWORKS["testnet"])
            horizon_url = os.getenv("STELLAR_HORIZON_URL", net_cfg["horizon"])
            self._usdc_issuer = os.getenv("STELLAR_USDC_ISSUER", net_cfg["usdc_issuer"])
            self._server = Server(horizon_url)

            log.info("StellarDEX agent started: assets=%s network=%s interval=%.0fs",
                     self._assets, self._network, self._interval)

        except ImportError:
            log.warning("stellar-sdk not installed — StellarDEX running in mock mode")
            await self._run_mock()
            return

        while not self._stop:
            try:
                await self._tick()
                self._tick_count += 1
                self._last_tick = time.time()
            except Exception as e:
                self._errors += 1
                log.warning("StellarDEX tick error: %s", e)

            await asyncio.sleep(self._interval)

    async def _tick(self):
        """Single polling tick: fetch SDEX data and publish."""
        if not self._server:
            return

        from stellar_sdk import Asset

        for asset_code in self._assets:
            if asset_code.upper() == "XLM":
                selling = Asset.native()
                buying = Asset("USDC", self._usdc_issuer)
            else:
                selling = Asset(asset_code, self._usdc_issuer)
                buying = Asset("USDC", self._usdc_issuer)

            try:
                orderbook = self._server.orderbook(selling, buying).limit(20).call()
                bids = orderbook.get("bids", [])
                asks = orderbook.get("asks", [])

                if not bids or not asks:
                    continue

                best_bid = float(bids[0]["price"])
                best_ask = float(asks[0]["price"])
                mid = (best_bid + best_ask) / 2
                spread_bps = ((best_ask - best_bid) / mid) * 10000

                # Calculate volume from depth
                bid_depth = sum(float(b["amount"]) * float(b["price"]) for b in bids[:10])
                ask_depth = sum(float(a["amount"]) * float(a["price"]) for a in asks[:10])

                price = StellarPrice(
                    asset=asset_code,
                    price_usd=mid,
                    bid=best_bid,
                    ask=best_ask,
                    spread_bps=round(spread_bps, 2),
                    volume_24h=bid_depth + ask_depth,
                )
                self._prices[asset_code] = price

                # Publish price to bus (same format as other scouts)
                await self.bus.publish("stellar.price", {
                    "asset": asset_code,
                    "price": mid,
                    "bid": best_bid,
                    "ask": best_ask,
                    "spread_bps": spread_bps,
                    "source": "sdex",
                    "network": self._network,
                    "ts": time.time(),
                })

                # Also publish as MarketSnapshot for compatibility
                snap = MarketSnapshot(
                    symbol=asset_code,
                    price=mid,
                    bid=best_bid,
                    ask=best_ask,
                    volume=bid_depth + ask_depth,
                    ts=time.time(),
                    source="stellar_dex",
                )
                await self.bus.publish("market", snap)

                # Publish orderbook depth
                await self.bus.publish("stellar.orderbook", {
                    "asset": asset_code,
                    "bids": [{"price": float(b["price"]), "amount": float(b["amount"])}
                             for b in bids[:10]],
                    "asks": [{"price": float(a["price"]), "amount": float(a["amount"])}
                             for a in asks[:10]],
                    "bid_depth_usd": round(bid_depth, 2),
                    "ask_depth_usd": round(ask_depth, 2),
                    "spread_bps": round(spread_bps, 2),
                    "ts": time.time(),
                })

                # Alert on wide spreads (arb opportunity)
                if spread_bps > 50:
                    await self.bus.publish("stellar.spread", {
                        "asset": asset_code,
                        "spread_bps": spread_bps,
                        "bid": best_bid,
                        "ask": best_ask,
                        "alert": "wide_spread",
                    })

            except Exception as e:
                log.warning("SDEX fetch failed for %s: %s", asset_code, e)

    async def _run_mock(self):
        """Mock mode: simulate SDEX prices for demo/testing."""
        import random
        base_prices = {"XLM": 0.12, "BTC": 65000.0, "ETH": 3200.0}

        while not self._stop:
            for asset_code in self._assets:
                base = base_prices.get(asset_code, 1.0)
                noise = random.uniform(-0.02, 0.02)
                mid = base * (1 + noise)
                spread = mid * 0.003  # 30bps spread
                bid = mid - spread / 2
                ask = mid + spread / 2

                snap = MarketSnapshot(
                    symbol=asset_code,
                    price=mid,
                    bid=bid,
                    ask=ask,
                    volume=random.uniform(50000, 200000),
                    ts=time.time(),
                    source="stellar_dex_mock",
                )
                await self.bus.publish("market", snap)
                await self.bus.publish("stellar.price", {
                    "asset": asset_code,
                    "price": mid,
                    "bid": bid,
                    "ask": ask,
                    "spread_bps": 30.0,
                    "source": "sdex_mock",
                    "network": self._network,
                    "ts": time.time(),
                })

                self._tick_count += 1
                self._last_tick = time.time()

            await asyncio.sleep(self._interval)

    async def _on_trade_request(self, msg: dict):
        """Handle a trade execution request on SDEX."""
        asset = msg.get("asset", "XLM")
        side = msg.get("side", "buy")  # "buy" or "sell"
        amount = msg.get("amount", 0.0)
        price = msg.get("price", 0.0)  # 0 = market order

        log.info("SDEX trade request: %s %.4f %s @ %s",
                 side, amount, asset, price or "market")

        # Publish trade intent for the gateway to execute
        await self.bus.publish("stellar.trade", {
            "asset": asset,
            "side": side,
            "amount": amount,
            "price": price,
            "status": "pending",
            "ts": time.time(),
        })

    async def find_path_payment(
        self,
        source_asset: str,
        dest_asset: str,
        amount: float,
    ) -> dict:
        """Find optimal path payment route on Stellar for multi-hop swaps."""
        if not self._server:
            return {"error": "not initialized"}

        try:
            from stellar_sdk import Asset

            if source_asset == "XLM":
                src = Asset.native()
            else:
                src = Asset(source_asset, self._usdc_issuer)

            if dest_asset == "XLM":
                dst = Asset.native()
            elif dest_asset == "USDC":
                dst = Asset("USDC", self._usdc_issuer)
            else:
                dst = Asset(dest_asset, self._usdc_issuer)

            paths = self._server.strict_receive_paths(
                source=[src],
                destination_asset=dst,
                destination_amount=f"{amount:.7f}",
            ).limit(5).call()

            records = paths.get("_embedded", {}).get("records", [])
            results = []
            for path in records:
                results.append({
                    "source_amount": path.get("source_amount"),
                    "destination_amount": path.get("destination_amount"),
                    "path": [
                        p.get("asset_code", "XLM") for p in path.get("path", [])
                    ],
                })

            return {
                "source": source_asset,
                "destination": dest_asset,
                "paths": results,
                "best_rate": float(results[0]["source_amount"]) / amount if results else 0,
            }

        except Exception as e:
            log.warning("Path payment query failed: %s", e)
            return {"error": str(e)}

    def stop(self):
        self._stop = True

    def status(self) -> dict:
        return {
            "agent": self.name,
            "network": self._network,
            "assets": self._assets,
            "ticks": self._tick_count,
            "errors": self._errors,
            "last_tick": self._last_tick,
            "prices": {
                k: {"price": round(v.price_usd, 6), "spread_bps": v.spread_bps}
                for k, v in self._prices.items()
            },
        }
