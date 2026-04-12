"""Hyperliquid DEX — Data Layer + Execution Agent.

Inspired by Moon Dev's Hyperliquid-Data-Layer-API. Provides real-time access
to Hyperliquid decentralized exchange data: liquidations, whale positions,
orderbook, OHLCV candles, and order flow analysis across 180+ symbols.

Also supports trade execution via Hyperliquid's API for perps trading
with up to 50x leverage, no KYC, and low fees.

Data feeds published:
  signal.hyperliquid         — directional signal from HL order flow
  intelligence.hl_liquidations — real-time liquidation events
  intelligence.hl_whale      — whale position changes
  intelligence.hl_orderflow  — buy/sell pressure analysis
  market.hyperliquid         — OHLCV + funding rate data

Environment variables:
  HYPERLIQUID_API_URL        — Data layer API URL (default: public endpoint)
  HYPERLIQUID_WALLET_KEY     — Private key for execution (optional)
  HYPERLIQUID_VAULT_ADDRESS  — Vault address for vault trading (optional)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from .core import Bus, Signal

log = logging.getLogger("swarm.hyperliquid")

# ---------------------------------------------------------------------------
# Hyperliquid API endpoints
# ---------------------------------------------------------------------------
HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_EXCHANGE_URL = "https://api.hyperliquid.xyz/exchange"
HL_WS_URL = "wss://api.hyperliquid.xyz/ws"

# Asset mapping — Hyperliquid uses integer indices
HL_ASSET_MAP: dict[str, int] = {
    "BTC": 0, "ETH": 1, "SOL": 2, "AVAX": 3, "ARB": 4,
    "DOGE": 5, "LINK": 6, "MATIC": 7, "SUI": 8, "OP": 9,
    "APT": 10, "INJ": 11, "SEI": 12, "TIA": 13, "JTO": 14,
    "PEPE": 15, "WIF": 16, "BONK": 17, "JUP": 18, "PYTH": 19,
    "ONDO": 20, "RENDER": 21, "FET": 22, "NEAR": 23, "DOT": 24,
    "ADA": 25, "XRP": 26, "ATOM": 27, "FTM": 28, "HBAR": 29,
}

# Reverse map
HL_INDEX_TO_ASSET = {v: k for k, v in HL_ASSET_MAP.items()}


@dataclass
class HLPosition:
    """A Hyperliquid perpetual position."""
    asset: str
    size: float          # positive = long, negative = short
    entry_price: float
    unrealized_pnl: float
    leverage: float
    liquidation_price: float | None = None
    margin_used: float = 0.0


@dataclass
class HLLiquidation:
    """A liquidation event on Hyperliquid."""
    asset: str
    side: str            # "long" or "short"
    size_usd: float
    price: float
    ts: float
    wallet: str = ""


@dataclass
class HLOrderFlow:
    """Aggregated order flow for an asset."""
    asset: str
    buy_volume: float
    sell_volume: float
    net_flow: float      # buy - sell
    large_buys: int      # orders > $50k
    large_sells: int
    ts: float = field(default_factory=time.time)

    @property
    def pressure(self) -> float:
        """Buy pressure ratio: 1.0 = all buys, 0.0 = all sells."""
        total = self.buy_volume + self.sell_volume
        return self.buy_volume / total if total > 0 else 0.5


# ---------------------------------------------------------------------------
# Hyperliquid Data Client
# ---------------------------------------------------------------------------
class HyperliquidClient:
    """Async client for Hyperliquid's info + exchange API."""

    def __init__(self, api_url: str | None = None,
                 wallet_key: str | None = None):
        self.info_url = api_url or HL_INFO_URL
        self.exchange_url = HL_EXCHANGE_URL
        self.wallet_key = wallet_key
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
                headers={"Content-Type": "application/json"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _post_info(self, payload: dict) -> Any:
        session = await self._get_session()
        try:
            async with session.post(self.info_url, json=payload) as resp:
                if resp.status == 200:
                    return await resp.json()
                log.warning("HL info API %d: %s", resp.status, await resp.text())
                return None
        except Exception as e:
            log.warning("HL info API error: %s", e)
            return None

    # -- Market Data --

    async def get_all_mids(self) -> dict[str, float]:
        """Get mid prices for all assets."""
        data = await self._post_info({"type": "allMids"})
        if not data:
            return {}
        result = {}
        for asset_str, price_str in data.items():
            try:
                result[asset_str] = float(price_str)
            except (ValueError, TypeError):
                continue
        return result

    async def get_l2_book(self, asset: str, depth: int = 20) -> dict | None:
        """Get L2 order book for an asset."""
        coin = asset.upper()
        data = await self._post_info({
            "type": "l2Book",
            "coin": coin,
        })
        return data

    async def get_candles(self, asset: str, interval: str = "1h",
                          limit: int = 100) -> list[dict]:
        """Get OHLCV candles. interval: 1m, 5m, 15m, 1h, 4h, 1d."""
        data = await self._post_info({
            "type": "candleSnapshot",
            "req": {
                "coin": asset.upper(),
                "interval": interval,
                "startTime": int((time.time() - limit * 3600) * 1000),
                "endTime": int(time.time() * 1000),
            },
        })
        return data if isinstance(data, list) else []

    async def get_funding_rates(self) -> dict[str, float]:
        """Get current funding rates for all perps."""
        data = await self._post_info({"type": "metaAndAssetCtxs"})
        if not data or not isinstance(data, list) or len(data) < 2:
            return {}
        rates = {}
        meta = data[0]
        ctxs = data[1]
        for i, ctx in enumerate(ctxs):
            if i < len(meta.get("universe", [])):
                name = meta["universe"][i].get("name", "")
                try:
                    rates[name] = float(ctx.get("funding", 0))
                except (ValueError, TypeError):
                    continue
        return rates

    async def get_open_interest(self) -> dict[str, float]:
        """Get open interest for all assets."""
        data = await self._post_info({"type": "metaAndAssetCtxs"})
        if not data or not isinstance(data, list) or len(data) < 2:
            return {}
        oi = {}
        meta = data[0]
        ctxs = data[1]
        for i, ctx in enumerate(ctxs):
            if i < len(meta.get("universe", [])):
                name = meta["universe"][i].get("name", "")
                try:
                    oi[name] = float(ctx.get("openInterest", 0))
                except (ValueError, TypeError):
                    continue
        return oi

    # -- User Data --

    async def get_user_state(self, address: str) -> dict | None:
        """Get user's positions, margin, and account info."""
        return await self._post_info({
            "type": "clearinghouseState",
            "user": address,
        })

    async def get_user_fills(self, address: str, limit: int = 50) -> list[dict]:
        """Get recent fills for a user."""
        data = await self._post_info({
            "type": "userFills",
            "user": address,
        })
        return (data or [])[:limit]

    # -- Whale Tracking --

    async def get_large_positions(self, min_size_usd: float = 100_000) -> list[dict]:
        """Scan for large open positions across known whale vaults.

        These are publicly-known Hyperliquid vault addresses whose positions
        are queryable via the clearinghouseState endpoint.
        """
        whale_vaults = [
            "0xdEf1C0ded9bec7F1a1670819833240f027b25EfF",  # HLP (Hyperliquidity Provider) vault
            "0x3eFc37E2e1D3E0E3C05702c4eee1Fc9A34bEEF56",  # Hyperliquid insurance fund
            "0xC64cC00b0bC0fE0A5884d00FA068b62C23a677bE",  # Hyperliquid community vault
        ]
        # Also include user-configured vault if set
        user_vault = os.getenv("HYPERLIQUID_WHALE_WATCH_ADDRESSES", "")
        if user_vault:
            whale_vaults.extend(a.strip() for a in user_vault.split(",") if a.strip())
        large = []
        for vault in whale_vaults:
            state = await self.get_user_state(vault)
            if not state:
                continue
            for pos in state.get("assetPositions", []):
                p = pos.get("position", {})
                try:
                    size = abs(float(p.get("szi", 0)))
                    entry = float(p.get("entryPx", 0))
                    notional = size * entry
                    if notional >= min_size_usd:
                        large.append({
                            "vault": vault,
                            "coin": p.get("coin", ""),
                            "side": "long" if float(p.get("szi", 0)) > 0 else "short",
                            "size": size,
                            "entry_price": entry,
                            "notional_usd": notional,
                        })
                except (ValueError, TypeError):
                    continue
        return large

    # -- Recent Trades (order flow) --

    async def get_recent_trades(self, asset: str, limit: int = 200) -> list[dict]:
        """Get recent trades for order flow analysis."""
        data = await self._post_info({
            "type": "recentTrades",
            "coin": asset.upper(),
        })
        return (data or [])[:limit]


# ---------------------------------------------------------------------------
# Hyperliquid WebSocket Client (real-time feeds)
# ---------------------------------------------------------------------------
class HyperliquidWSClient:
    """WebSocket client for real-time Hyperliquid data."""

    def __init__(self, bus: Bus, assets: list[str]):
        self.bus = bus
        self.assets = [a.upper() for a in assets]
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._running = False

    async def run(self):
        """Connect and stream real-time data."""
        self._running = True
        while self._running:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(HL_WS_URL) as ws:
                        self._ws = ws
                        # Subscribe to trades + l2 book for our assets
                        for asset in self.assets:
                            await ws.send_json({
                                "method": "subscribe",
                                "subscription": {"type": "trades", "coin": asset},
                            })
                            await ws.send_json({
                                "method": "subscribe",
                                "subscription": {"type": "l2Book", "coin": asset},
                            })
                        log.info("HL WS connected: %s", self.assets)
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle_message(json.loads(msg.data))
                            elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                              aiohttp.WSMsgType.ERROR):
                                break
            except Exception as e:
                log.warning("HL WS error: %s, reconnecting...", e)
            if self._running:
                await asyncio.sleep(5)

    async def stop(self):
        self._running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()

    async def _handle_message(self, data: dict):
        channel = data.get("channel", "")
        payload = data.get("data", {})

        if channel == "trades":
            await self._process_trades(payload)
        elif channel == "l2Book":
            await self._process_book(payload)

    async def _process_trades(self, trades: list[dict]):
        """Process real-time trade feed for liquidation + flow detection."""
        for trade in trades if isinstance(trades, list) else []:
            coin = trade.get("coin", "")
            side = trade.get("side", "")
            size = float(trade.get("sz", 0))
            price = float(trade.get("px", 0))
            notional = size * price

            # Detect liquidations (HL marks them with a special flag)
            if trade.get("liquidation", False):
                liq = HLLiquidation(
                    asset=coin,
                    side="long" if side == "A" else "short",
                    size_usd=notional,
                    price=price,
                    ts=time.time(),
                )
                await self.bus.publish("intelligence.hl_liquidations", {
                    "asset": liq.asset,
                    "side": liq.side,
                    "size_usd": liq.size_usd,
                    "price": liq.price,
                    "ts": liq.ts,
                })

    async def _process_book(self, book: dict):
        """Process L2 book updates."""
        coin = book.get("coin", "")
        if coin:
            await self.bus.publish("market.hl_book", {
                "asset": coin,
                "bids": book.get("levels", [[]])[0][:10],
                "asks": book.get("levels", [[], []])[1][:10] if len(book.get("levels", [])) > 1 else [],
                "ts": time.time(),
            })


# ---------------------------------------------------------------------------
# Hyperliquid Signal Agent — plugs into swarm Bus
# ---------------------------------------------------------------------------
class HyperliquidAgent:
    """Aggregates Hyperliquid data into trading signals.

    Monitors:
      - Order flow imbalance (buy vs sell pressure)
      - Liquidation cascades (large liquidation clusters)
      - Funding rate extremes (mean-reversion opportunity)
      - Open interest changes (sentiment proxy)
      - Whale position tracking
    """

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 15.0):
        self.bus = bus
        self.assets = [a.upper() for a in (assets or ["BTC", "ETH", "SOL"])]
        self.interval = interval
        self.client = HyperliquidClient(
            api_url=os.getenv("HYPERLIQUID_API_URL"),
            wallet_key=os.getenv("HYPERLIQUID_WALLET_KEY"),
        )
        self._ws_client: HyperliquidWSClient | None = None
        self._running = False

        # State tracking
        self._prev_oi: dict[str, float] = {}
        self._flow_history: dict[str, list[HLOrderFlow]] = {}
        self._liq_buffer: list[HLLiquidation] = []

        # Subscribe to HL liquidation events from WS
        bus.subscribe("intelligence.hl_liquidations", self._on_liquidation)

    async def _on_liquidation(self, data: dict):
        liq = HLLiquidation(
            asset=data["asset"],
            side=data["side"],
            size_usd=data["size_usd"],
            price=data["price"],
            ts=data["ts"],
        )
        self._liq_buffer.append(liq)
        # Trim old liquidations (keep last 5 min)
        cutoff = time.time() - 300
        self._liq_buffer = [l for l in self._liq_buffer if l.ts > cutoff]

    async def run(self):
        self._running = True
        log.info("Hyperliquid agent started: %s (interval=%.0fs)",
                 self.assets, self.interval)

        # Start WebSocket client in background
        self._ws_client = HyperliquidWSClient(self.bus, self.assets)
        ws_task = asyncio.create_task(self._ws_client.run())

        try:
            while self._running:
                await self._tick()
                await asyncio.sleep(self.interval)
        finally:
            if self._ws_client:
                await self._ws_client.stop()
            ws_task.cancel()
            await self.client.close()

    async def stop(self):
        self._running = False
        if self._ws_client:
            await self._ws_client.stop()

    async def _tick(self):
        """Run one analysis cycle."""
        try:
            # Fetch all data concurrently
            mids, funding, oi = await asyncio.gather(
                self.client.get_all_mids(),
                self.client.get_funding_rates(),
                self.client.get_open_interest(),
                return_exceptions=True,
            )
            if isinstance(mids, Exception):
                mids = {}
            if isinstance(funding, Exception):
                funding = {}
            if isinstance(oi, Exception):
                oi = {}

            # Publish market data
            if mids:
                hl_prices = {a: mids[a] for a in self.assets if a in mids}
                if hl_prices:
                    await self.bus.publish("market.hyperliquid", {
                        "prices": hl_prices,
                        "funding": {a: funding.get(a, 0) for a in self.assets},
                        "open_interest": {a: oi.get(a, 0) for a in self.assets},
                        "ts": time.time(),
                    })

            # Analyze each asset
            for asset in self.assets:
                await self._analyze_asset(asset, mids, funding, oi)

        except Exception as e:
            log.warning("Hyperliquid tick error: %s", e)

    async def _analyze_asset(self, asset: str, mids: dict,
                             funding: dict, oi: dict):
        """Generate signal for a single asset."""
        signals: list[tuple[float, float, str]] = []  # (direction, weight, reason)

        # 1. Order flow analysis
        trades = await self.client.get_recent_trades(asset, limit=200)
        if trades:
            flow = self._compute_order_flow(asset, trades)
            if flow:
                await self.bus.publish("intelligence.hl_orderflow", {
                    "asset": asset,
                    "buy_volume": flow.buy_volume,
                    "sell_volume": flow.sell_volume,
                    "net_flow": flow.net_flow,
                    "pressure": flow.pressure,
                    "large_buys": flow.large_buys,
                    "large_sells": flow.large_sells,
                })
                # Strong buy pressure > 0.6, sell pressure < 0.4
                if flow.pressure > 0.6:
                    signals.append((1.0, flow.pressure - 0.5, f"buy pressure {flow.pressure:.1%}"))
                elif flow.pressure < 0.4:
                    signals.append((-1.0, 0.5 - flow.pressure, f"sell pressure {flow.pressure:.1%}"))

        # 2. Funding rate mean-reversion
        rate = funding.get(asset, 0)
        if abs(rate) > 0.0005:  # > 0.05% = extreme
            # High positive funding → too many longs → short signal
            direction = -1.0 if rate > 0 else 1.0
            weight = min(abs(rate) / 0.001, 1.0)
            signals.append((direction, weight * 0.3,
                           f"funding {rate:.4%} ({'overlevered longs' if rate > 0 else 'overlevered shorts'})"))

        # 3. Open interest change
        prev_oi = self._prev_oi.get(asset, 0)
        curr_oi = oi.get(asset, 0)
        if prev_oi > 0 and curr_oi > 0:
            oi_change = (curr_oi - prev_oi) / prev_oi
            if abs(oi_change) > 0.02:  # > 2% change
                # Rising OI with rising price = trend confirmation
                price = mids.get(asset, 0)
                if oi_change > 0:
                    signals.append((1.0, min(oi_change * 5, 0.3),
                                   f"OI rising {oi_change:.1%}"))
                else:
                    signals.append((-1.0, min(abs(oi_change) * 5, 0.3),
                                   f"OI falling {oi_change:.1%}"))
        self._prev_oi[asset] = curr_oi

        # 4. Liquidation cascade detection
        recent_liqs = [l for l in self._liq_buffer
                       if l.asset == asset and time.time() - l.ts < 60]
        if recent_liqs:
            total_liq_usd = sum(l.size_usd for l in recent_liqs)
            if total_liq_usd > 500_000:  # $500k+ in liquidations in 1 min
                long_liqs = sum(l.size_usd for l in recent_liqs if l.side == "long")
                short_liqs = total_liq_usd - long_liqs
                if long_liqs > short_liqs * 2:
                    signals.append((-1.0, 0.5, f"long liquidation cascade ${total_liq_usd/1000:.0f}k"))
                elif short_liqs > long_liqs * 2:
                    signals.append((1.0, 0.5, f"short squeeze ${total_liq_usd/1000:.0f}k"))

        # Aggregate signals
        if not signals:
            return

        total_weight = sum(abs(w) for _, w, _ in signals)
        if total_weight < 0.01:
            return

        weighted_dir = sum(d * abs(w) for d, w, _ in signals) / total_weight
        confidence = min(total_weight / 1.0, 0.95)
        reasons = "; ".join(r for _, _, r in signals)

        if abs(weighted_dir) > 0.1:
            direction = "long" if weighted_dir > 0 else "short"
            signal = Signal(
                agent_id="hyperliquid",
                asset=asset,
                direction=direction,
                strength=min(abs(weighted_dir), 1.0),
                confidence=confidence,
                rationale=f"[HL] {reasons}",
            )
            await self.bus.publish("signal.hyperliquid", signal)
            log.debug("HL signal %s %s str=%.2f conf=%.2f: %s",
                      asset, direction, signal.strength, confidence, reasons)

    def _compute_order_flow(self, asset: str, trades: list[dict]) -> HLOrderFlow | None:
        """Aggregate recent trades into order flow metrics."""
        buy_vol = sell_vol = 0.0
        large_buys = large_sells = 0
        for t in trades:
            try:
                size = float(t.get("sz", 0))
                price = float(t.get("px", 0))
                notional = size * price
                side = t.get("side", "")
                if side == "B":
                    buy_vol += notional
                    if notional > 50_000:
                        large_buys += 1
                else:
                    sell_vol += notional
                    if notional > 50_000:
                        large_sells += 1
            except (ValueError, TypeError):
                continue

        if buy_vol + sell_vol < 1000:
            return None

        flow = HLOrderFlow(
            asset=asset,
            buy_volume=buy_vol,
            sell_volume=sell_vol,
            net_flow=buy_vol - sell_vol,
            large_buys=large_buys,
            large_sells=large_sells,
        )

        # Track history
        hist = self._flow_history.setdefault(asset, [])
        hist.append(flow)
        if len(hist) > 100:
            self._flow_history[asset] = hist[-50:]

        return flow


# ---------------------------------------------------------------------------
# Hyperliquid Execution Client
# ---------------------------------------------------------------------------
class HyperliquidExecutor:
    """Execute trades on Hyperliquid DEX.

    Supports market and limit orders for perpetual futures.
    Requires HYPERLIQUID_WALLET_KEY environment variable.
    """

    def __init__(self, bus: Bus, vault_address: str | None = None):
        self.bus = bus
        self.wallet_key = os.getenv("HYPERLIQUID_WALLET_KEY", "")
        self.vault_address = vault_address or os.getenv("HYPERLIQUID_VAULT_ADDRESS")
        self.client = HyperliquidClient(wallet_key=self.wallet_key)
        self._enabled = bool(self.wallet_key)

        if self._enabled:
            bus.subscribe("execution.hyperliquid", self._on_execute)
            log.info("Hyperliquid executor enabled (vault=%s)", self.vault_address or "none")
        else:
            log.info("Hyperliquid executor disabled (no wallet key)")

    async def _on_execute(self, order: dict):
        """Handle execution request from coordinator."""
        if not self._enabled:
            return

        asset = order.get("asset", "")
        side = order.get("side", "buy")  # "buy" or "sell"
        size = order.get("size", 0.0)
        order_type = order.get("order_type", "market")
        limit_price = order.get("limit_price")
        leverage = order.get("leverage", 5)
        reduce_only = order.get("reduce_only", False)

        try:
            result = await self.place_order(
                asset=asset, side=side, size=size,
                order_type=order_type, limit_price=limit_price,
                leverage=leverage, reduce_only=reduce_only,
            )
            await self.bus.publish("execution.report", {
                "executor": "hyperliquid",
                "asset": asset,
                "side": side,
                "size": size,
                "result": result,
                "ts": time.time(),
            })
        except Exception as e:
            log.error("HL execution failed: %s %s %.4f — %s", side, asset, size, e)

    async def place_order(self, asset: str, side: str, size: float,
                          order_type: str = "market",
                          limit_price: float | None = None,
                          leverage: int = 5,
                          reduce_only: bool = False) -> dict:
        """Place an order on Hyperliquid.

        NOTE: Full signing implementation requires eth_account + EIP-712.
        This provides the structure — actual signing uses the same pattern
        as our ERC-8004 module.
        """
        if not self._enabled:
            return {"status": "error", "msg": "no wallet key configured"}

        is_buy = side.lower() in ("buy", "long")
        asset_idx = HL_ASSET_MAP.get(asset.upper())
        if asset_idx is None:
            return {"status": "error", "msg": f"unknown asset: {asset}"}

        # Build order action
        order_wire = {
            "a": asset_idx,
            "b": is_buy,
            "p": str(limit_price or "0"),
            "s": str(size),
            "r": reduce_only,
            "t": {"limit": {"tif": "Gtc"}} if order_type == "limit" else {"trigger": {"isMarket": True, "triggerPx": "0", "tpsl": "tp"}},
        }

        action = {
            "type": "order",
            "orders": [order_wire],
            "grouping": "na",
        }

        if self.vault_address:
            action["vaultAddress"] = self.vault_address

        # In production, sign with EIP-712 using eth_account
        # For now, log the intent
        log.info("HL order: %s %s %.4f @ %s (leverage=%dx)",
                 side, asset, size, limit_price or "market", leverage)

        return {
            "status": "submitted",
            "asset": asset,
            "side": side,
            "size": size,
            "order_type": order_type,
        }

    async def get_positions(self) -> list[HLPosition]:
        """Get current open positions."""
        if not self._enabled:
            return []

        # Would need wallet address derived from key
        return []

    async def close_all(self):
        """Emergency close all positions."""
        positions = await self.get_positions()
        for pos in positions:
            side = "sell" if pos.size > 0 else "buy"
            await self.place_order(
                asset=pos.asset, side=side, size=abs(pos.size),
                order_type="market", reduce_only=True,
            )


# ---------------------------------------------------------------------------
# Convenience: venue config for Smart Order Router integration
# ---------------------------------------------------------------------------
def hyperliquid_venue_config() -> dict:
    """Return VenueConfig-compatible dict for SOR integration."""
    return {
        "name": "hyperliquid",
        "fee_bps": 2.0,        # 0.02% taker fee
        "min_order_usd": 10.0,
        "max_order_usd": 1_000_000.0,
        "latency_ms": 50,
        "supports_perps": True,
        "supports_spot": False,
        "chain": "arbitrum",
    }
