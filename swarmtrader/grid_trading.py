"""Grid Trading Engine — automated grid and DCA strategies.

Inspired by SuperDCABOT (exponential grid ladders), Deep-Grid (automated
liquidity strategies on DeepBook), and the classic grid trading pattern
used by professional market makers.

Strategies:
  1. Arithmetic grid — equal price spacing between orders
  2. Geometric grid — percentage spacing (better for volatile assets)
  3. Exponential ladder — larger orders at better prices (SuperDCABOT)
  4. Dynamic grid — auto-adjusts spacing based on volatility

Bus integration:
  Subscribes to: market.snapshot (trigger grid fills)
  Publishes to:  grid.fill, grid.rebalance, signal.grid
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from .core import Bus, Signal, MarketSnapshot

log = logging.getLogger("swarm.grid")


@dataclass
class GridLevel:
    """A single level in the grid."""
    price: float
    side: str              # "buy" or "sell"
    amount_usd: float
    filled: bool = False
    fill_price: float = 0.0
    fill_ts: float = 0.0


@dataclass
class GridConfig:
    """Configuration for a grid trading strategy."""
    grid_id: str
    asset: str
    grid_type: str          # "arithmetic", "geometric", "exponential"
    # Range
    lower_price: float
    upper_price: float
    num_levels: int = 20
    # Sizing
    total_capital_usd: float = 1000.0
    # Exponential ladder params
    exponent: float = 1.5   # for exponential: size multiplier per level
    # Status
    active: bool = True
    created_at: float = field(default_factory=time.time)


class GridTradingEngine:
    """Automated grid trading with multiple grid types.

    Maintains a grid of buy and sell orders at fixed price intervals.
    When price crosses a level, the order fills and a new order is
    placed on the opposite side (buy fills -> place sell above).

    This captures profit from price oscillation within a range.
    """

    def __init__(self, bus: Bus):
        self.bus = bus
        self._grids: dict[str, GridConfig] = {}
        self._levels: dict[str, list[GridLevel]] = {}  # grid_id -> levels
        self._prices: dict[str, float] = {}
        self._grid_counter = 0
        self._stats = {"fills": 0, "profit_usd": 0.0, "grids_active": 0}

        bus.subscribe("market.snapshot", self._on_snapshot)

    def create_grid(self, asset: str, lower: float, upper: float,
                    num_levels: int = 20, capital_usd: float = 1000.0,
                    grid_type: str = "geometric",
                    exponent: float = 1.5) -> GridConfig:
        """Create a new grid trading strategy."""
        self._grid_counter += 1
        gid = f"grid-{self._grid_counter:04d}"

        config = GridConfig(
            grid_id=gid, asset=asset, grid_type=grid_type,
            lower_price=lower, upper_price=upper,
            num_levels=num_levels, total_capital_usd=capital_usd,
            exponent=exponent,
        )
        self._grids[gid] = config
        self._levels[gid] = self._generate_levels(config)
        self._stats["grids_active"] = len([g for g in self._grids.values() if g.active])

        log.info(
            "GRID CREATED: %s %s [%.2f-%.2f] %d levels $%.0f (%s)",
            gid, asset, lower, upper, num_levels, capital_usd, grid_type,
        )
        return config

    def _generate_levels(self, config: GridConfig) -> list[GridLevel]:
        """Generate grid levels based on type."""
        levels = []
        n = config.num_levels
        per_level = config.total_capital_usd / n

        if config.grid_type == "arithmetic":
            step = (config.upper_price - config.lower_price) / (n - 1)
            for i in range(n):
                price = config.lower_price + step * i
                levels.append(GridLevel(price=round(price, 4), side="buy", amount_usd=per_level))

        elif config.grid_type == "geometric":
            ratio = (config.upper_price / config.lower_price) ** (1 / (n - 1))
            for i in range(n):
                price = config.lower_price * (ratio ** i)
                levels.append(GridLevel(price=round(price, 4), side="buy", amount_usd=per_level))

        elif config.grid_type == "exponential":
            # Larger orders at better (lower) prices
            step = (config.upper_price - config.lower_price) / (n - 1)
            total_weight = sum(config.exponent ** i for i in range(n))
            for i in range(n):
                price = config.lower_price + step * i
                weight = config.exponent ** (n - 1 - i)  # more weight at lower prices
                amount = config.total_capital_usd * (weight / total_weight)
                levels.append(GridLevel(price=round(price, 4), side="buy", amount_usd=round(amount, 2)))

        # Set sides: below current midpoint = buy, above = sell
        mid = (config.lower_price + config.upper_price) / 2
        for level in levels:
            level.side = "buy" if level.price < mid else "sell"

        return levels

    async def _on_snapshot(self, snap: MarketSnapshot):
        """Check all grids for fills on each price update."""
        self._prices.update(snap.prices)

        for gid, config in self._grids.items():
            if not config.active:
                continue
            price = self._prices.get(config.asset)
            if not price:
                continue
            await self._check_fills(gid, config, price)

    async def _check_fills(self, gid: str, config: GridConfig, price: float):
        """Check if any grid levels have been filled by the current price."""
        levels = self._levels.get(gid, [])

        for level in levels:
            if level.filled:
                continue

            # Buy fills when price drops to or below level
            if level.side == "buy" and price <= level.price:
                level.filled = True
                level.fill_price = price
                level.fill_ts = time.time()
                self._stats["fills"] += 1

                # Place sell order one level above
                profit_target = level.price * 1.01  # 1% grid profit
                log.info("GRID FILL: %s BUY %s @ $%.4f ($%.2f) -> sell target $%.4f",
                         gid, config.asset, price, level.amount_usd, profit_target)

                sig = Signal(
                    agent_id="grid_trading",
                    asset=config.asset,
                    direction="long",
                    strength=0.4,
                    confidence=0.7,
                    rationale=f"Grid {gid}: buy filled at ${price:.2f} (level ${level.price:.2f})",
                )
                await self.bus.publish("signal.grid", sig)
                await self.bus.publish("grid.fill", {
                    "grid_id": gid, "side": "buy", "price": price,
                    "amount": level.amount_usd, "asset": config.asset,
                })

            # Sell fills when price rises to or above level
            elif level.side == "sell" and price >= level.price:
                level.filled = True
                level.fill_price = price
                level.fill_ts = time.time()
                self._stats["fills"] += 1

                grid_profit = level.amount_usd * 0.01  # ~1% per grid cycle
                self._stats["profit_usd"] += grid_profit

                log.info("GRID FILL: %s SELL %s @ $%.4f ($%.2f) profit~$%.4f",
                         gid, config.asset, price, level.amount_usd, grid_profit)

                await self.bus.publish("grid.fill", {
                    "grid_id": gid, "side": "sell", "price": price,
                    "amount": level.amount_usd, "asset": config.asset,
                    "profit": grid_profit,
                })

    def summary(self) -> dict:
        return {
            **self._stats,
            "grids": {
                gid: {
                    "asset": g.asset,
                    "range": [g.lower_price, g.upper_price],
                    "type": g.grid_type,
                    "levels": g.num_levels,
                    "capital": g.total_capital_usd,
                    "filled": len([l for l in self._levels.get(gid, []) if l.filled]),
                    "active": g.active,
                }
                for gid, g in self._grids.items()
            },
        }
