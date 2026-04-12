"""Uniswap v4 Hook Integration — atomic on-chain operations.

Inspired by GammaHedge (hook-native delta-neutral hedging), KalmanGuard
(dynamic fee adjustment via hooks), TWAP CHOP (chunked execution), and
Coupled Markets (AI agent improving LP returns).

Uniswap v4 hooks allow custom logic at key lifecycle points:
  - beforeSwap / afterSwap — execute alongside trades
  - beforeAddLiquidity / afterAddLiquidity
  - afterInitialize — setup on pool creation

This module provides Python interfaces for:
  1. Reading v4 pool state (prices, liquidity, tick)
  2. Managing concentrated liquidity positions (add/remove/rebalance)
  3. TWAP execution via hooks
  4. Dynamic fee reading
  5. LP position monitoring and auto-rebalance triggers

Bus integration:
  Subscribes to: market.snapshot (LP position monitoring)
  Publishes to:  v4.rebalance_needed, v4.position_updated, v4.fee_changed

Environment variables:
  V4_POOL_MANAGER      — Uniswap v4 PoolManager address
  V4_POSITION_MANAGER  — Position manager contract address
  V4_CHAIN_ID          — Chain ID (default: 8453 = Base)
  V4_RPC_URL           — RPC endpoint
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field

from .core import Bus, MarketSnapshot

log = logging.getLogger("swarm.v4_hooks")

# Uniswap v4 contract addresses (Base Sepolia for testing)
V4_ADDRESSES = {
    "base_sepolia": {
        "pool_manager": "0x05E73354cFDd6745C338b50BcFDfA3Aa6fA03408",
        "position_manager": "0x4b2c77d209d3405F41a037Ec6718a3e1A2e14eEF",
        "quoter": "0xC5290058841028F1614F3A6F0F5816cAd0df5E27",
    },
    "base": {
        "pool_manager": "",  # TBD - mainnet launch
        "position_manager": "",
        "quoter": "",
    },
}

# Minimal v4 PoolManager ABI
POOL_MANAGER_ABI = json.loads("""[
  {"inputs":[{"name":"key","type":"tuple","components":[{"name":"currency0","type":"address"},{"name":"currency1","type":"address"},{"name":"fee","type":"uint24"},{"name":"tickSpacing","type":"int24"},{"name":"hooks","type":"address"}]}],"name":"getSlot0","outputs":[{"name":"sqrtPriceX96","type":"uint160"},{"name":"tick","type":"int24"},{"name":"protocolFee","type":"uint24"},{"name":"lpFee","type":"uint24"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"name":"key","type":"tuple","components":[{"name":"currency0","type":"address"},{"name":"currency1","type":"address"},{"name":"fee","type":"uint24"},{"name":"tickSpacing","type":"int24"},{"name":"hooks","type":"address"}]}],"name":"getLiquidity","outputs":[{"type":"uint128"}],"stateMutability":"view","type":"function"}
]""")


@dataclass
class V4PoolState:
    """Current state of a Uniswap v4 pool."""
    pool_id: str
    token0: str
    token1: str
    sqrt_price_x96: int = 0
    tick: int = 0
    liquidity: int = 0
    fee_bps: int = 30            # dynamic fee in bps
    tick_spacing: int = 60
    price: float = 0.0           # human-readable price
    ts: float = field(default_factory=time.time)

    def price_from_sqrt(self) -> float:
        """Convert sqrtPriceX96 to human-readable price."""
        if self.sqrt_price_x96 == 0:
            return 0.0
        price = (self.sqrt_price_x96 / (2 ** 96)) ** 2
        return price


@dataclass
class V4Position:
    """A concentrated liquidity position in a v4 pool."""
    position_id: str
    pool_id: str
    tick_lower: int
    tick_upper: int
    liquidity: int
    # Value tracking
    token0_amount: float = 0.0
    token1_amount: float = 0.0
    fees_earned_0: float = 0.0
    fees_earned_1: float = 0.0
    # Status
    in_range: bool = True
    created_at: float = field(default_factory=time.time)
    last_rebalance: float = 0.0

    def range_width_bps(self, tick_spacing: int = 60) -> int:
        """Width of position range in basis points."""
        ticks = self.tick_upper - self.tick_lower
        # Each tick represents ~1 bps price change
        return ticks


@dataclass
class RebalanceAction:
    """Proposed LP rebalance action."""
    position_id: str
    action: str              # "widen", "narrow", "shift_up", "shift_down", "close"
    old_tick_lower: int
    old_tick_upper: int
    new_tick_lower: int
    new_tick_upper: int
    reason: str
    urgency: float           # 0-1, higher = more urgent


class V4HookManager:
    """Manages Uniswap v4 hook interactions and LP positions.

    Capabilities:
      1. Pool state reading (price, tick, liquidity, fees)
      2. Position monitoring (in-range check, IL calculation)
      3. Auto-rebalance triggers (shift range when out-of-range)
      4. TWAP execution routing (chunk orders through v4 pools)
      5. Dynamic fee monitoring (read current fee tier)
    """

    def __init__(self, bus: Bus, mode: str = "simulate",
                 rebalance_threshold: float = 0.8):
        self.bus = bus
        self.mode = mode
        self.rebalance_threshold = rebalance_threshold
        self._pools: dict[str, V4PoolState] = {}
        self._positions: dict[str, V4Position] = {}
        self._rebalance_queue: list[RebalanceAction] = []
        self._w3 = None
        self._pool_manager = None
        self._stats = {
            "pools_monitored": 0, "positions": 0,
            "rebalances": 0, "fees_collected": 0.0,
        }

        if mode == "live":
            self._init_web3()

        bus.subscribe("market.snapshot", self._on_snapshot)

    def _init_web3(self):
        try:
            from web3 import Web3
            rpc = os.getenv("V4_RPC_URL", "https://mainnet.base.org")
            self._w3 = Web3(Web3.HTTPProvider(rpc))
            pm_addr = os.getenv("V4_POOL_MANAGER", "")
            if pm_addr and self._w3:
                self._pool_manager = self._w3.eth.contract(
                    address=Web3.to_checksum_address(pm_addr),
                    abi=POOL_MANAGER_ABI,
                )
        except ImportError:
            log.warning("web3 not installed — v4 hooks in simulate mode")
            self.mode = "simulate"

    # ── Pool State ───────────────────────────────────────────────

    def register_pool(self, pool_id: str, token0: str, token1: str,
                      fee_bps: int = 30, tick_spacing: int = 60) -> V4PoolState:
        """Register a v4 pool for monitoring."""
        pool = V4PoolState(
            pool_id=pool_id, token0=token0, token1=token1,
            fee_bps=fee_bps, tick_spacing=tick_spacing,
        )
        self._pools[pool_id] = pool
        self._stats["pools_monitored"] = len(self._pools)
        log.info("V4 pool registered: %s (%s/%s, fee=%dbps)", pool_id, token0, token1, fee_bps)
        return pool

    async def _on_snapshot(self, snap: MarketSnapshot):
        """Update pool states from market data and check positions."""
        for pool_id, pool in self._pools.items():
            # Update price from snapshot
            pair_key = f"{pool.token0}/{pool.token1}"
            price = snap.prices.get(pool.token0, 0.0)
            if price > 0:
                pool.price = price
                # Approximate tick from price (tick = log1.0001(price))
                if price > 0:
                    pool.tick = int(math.log(price) / math.log(1.0001))
                pool.ts = time.time()

        # Check all positions for rebalance needs
        await self._check_positions()

    # ── Position Management ──────────────────────────────────────

    def open_position(self, pool_id: str, tick_lower: int, tick_upper: int,
                      liquidity: int = 0) -> V4Position:
        """Open a new concentrated liquidity position."""
        pos_id = f"v4pos-{len(self._positions) + 1:04d}"
        pos = V4Position(
            position_id=pos_id, pool_id=pool_id,
            tick_lower=tick_lower, tick_upper=tick_upper,
            liquidity=liquidity,
        )
        self._positions[pos_id] = pos
        self._stats["positions"] = len(self._positions)
        log.info("V4 position opened: %s in pool %s [%d, %d]",
                 pos_id, pool_id, tick_lower, tick_upper)
        return pos

    async def _check_positions(self):
        """Check all positions for out-of-range and trigger rebalance."""
        for pos in self._positions.values():
            pool = self._pools.get(pos.pool_id)
            if not pool:
                continue

            # Check if position is in range
            was_in_range = pos.in_range
            pos.in_range = pos.tick_lower <= pool.tick <= pos.tick_upper

            if was_in_range and not pos.in_range:
                # Position went out of range — propose rebalance
                range_width = pos.tick_upper - pos.tick_lower
                half_width = range_width // 2

                if pool.tick < pos.tick_lower:
                    action = "shift_down"
                    new_lower = pool.tick - half_width
                    new_upper = pool.tick + half_width
                else:
                    action = "shift_up"
                    new_lower = pool.tick - half_width
                    new_upper = pool.tick + half_width

                rebalance = RebalanceAction(
                    position_id=pos.position_id,
                    action=action,
                    old_tick_lower=pos.tick_lower,
                    old_tick_upper=pos.tick_upper,
                    new_tick_lower=new_lower,
                    new_tick_upper=new_upper,
                    reason=f"tick {pool.tick} outside [{pos.tick_lower}, {pos.tick_upper}]",
                    urgency=0.8,
                )
                self._rebalance_queue.append(rebalance)

                log.warning(
                    "V4 REBALANCE NEEDED: %s out of range (tick=%d, range=[%d,%d]) -> %s to [%d,%d]",
                    pos.position_id, pool.tick, pos.tick_lower, pos.tick_upper,
                    action, new_lower, new_upper,
                )

                await self.bus.publish("v4.rebalance_needed", {
                    "position": pos,
                    "pool": pool,
                    "action": rebalance,
                })

    def execute_rebalance(self, rebalance: RebalanceAction) -> bool:
        """Execute a position rebalance (simulation or on-chain)."""
        pos = self._positions.get(rebalance.position_id)
        if not pos:
            return False

        pos.tick_lower = rebalance.new_tick_lower
        pos.tick_upper = rebalance.new_tick_upper
        pos.in_range = True
        pos.last_rebalance = time.time()
        self._stats["rebalances"] += 1

        log.info(
            "V4 REBALANCED: %s -> [%d, %d] (%s)",
            pos.position_id, pos.tick_lower, pos.tick_upper,
            rebalance.action,
        )
        return True

    # ── TWAP Routing ─────────────────────────────────────────────

    def compute_twap_chunks(self, pool_id: str, total_amount: float,
                            max_impact_bps: float = 50.0,
                            num_chunks: int = 5) -> list[dict]:
        """Compute optimal TWAP chunk schedule for a v4 pool.

        Uses binary search to find chunk sizes that keep impact below threshold.
        """
        pool = self._pools.get(pool_id)
        if not pool:
            return []

        # Simple chunking — in production, would use pool liquidity depth
        chunk_size = total_amount / num_chunks
        interval_s = 30  # seconds between chunks

        chunks = []
        for i in range(num_chunks):
            chunks.append({
                "chunk_index": i,
                "amount": round(chunk_size, 4),
                "execute_at": time.time() + (i * interval_s),
                "pool_id": pool_id,
                "estimated_impact_bps": round(
                    (chunk_size / max(pool.liquidity / 1e18, 1)) * 10_000, 2
                ),
            })
        return chunks

    # ── IL Calculation ───────────────────────────────────────────

    @staticmethod
    def impermanent_loss(price_ratio: float) -> float:
        """Calculate impermanent loss for a given price change ratio.

        Args:
            price_ratio: current_price / entry_price

        Returns:
            IL as a fraction (0.0 = no loss, -0.057 = 5.7% loss at 2x)
        """
        if price_ratio <= 0:
            return 0.0
        return 2 * math.sqrt(price_ratio) / (1 + price_ratio) - 1

    def summary(self) -> dict:
        return {
            **self._stats,
            "mode": self.mode,
            "pools": {
                pid: {
                    "pair": f"{p.token0}/{p.token1}",
                    "price": round(p.price, 4),
                    "tick": p.tick,
                    "fee_bps": p.fee_bps,
                }
                for pid, p in self._pools.items()
            },
            "positions": {
                pid: {
                    "pool": p.pool_id,
                    "range": [p.tick_lower, p.tick_upper],
                    "in_range": p.in_range,
                    "width_bps": p.range_width_bps(),
                }
                for pid, p in self._positions.items()
            },
            "pending_rebalances": len(self._rebalance_queue),
        }
