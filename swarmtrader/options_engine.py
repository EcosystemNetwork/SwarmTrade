"""Options Strategy Automation — automated options trading and hedging.

Inspired by VeraFi (TEE-powered options market maker), OpSwap (options
via Uniswap V4 hooks), Salona (covered calls for BTC holders), and
the existing Deribit integration for options IV data.

Strategies:
  1. Covered calls — sell calls against long spot positions for income
  2. Protective puts — buy puts to hedge downside on existing longs
  3. Straddles — buy call+put when expecting big move (either direction)
  4. Iron condors — sell call spread + put spread for range-bound markets
  5. Vol arbitrage — trade IV vs realized vol divergence

Uses existing Deribit options data (IV, put/call ratio, max pain)
from the feeds module to inform strategy selection.

Bus integration:
  Subscribes to: signal.options, signal.regime, market.snapshot
  Publishes to:  options.strategy, options.execution, signal.options_strategy
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from .core import Bus, Signal, MarketSnapshot

log = logging.getLogger("swarm.options")


@dataclass
class OptionsStrategy:
    """A proposed options strategy."""
    strategy_id: str
    name: str               # "covered_call", "protective_put", "straddle", etc.
    asset: str
    direction: str          # "bullish", "bearish", "neutral", "vol_long"
    # Legs
    legs: list[dict] = field(default_factory=list)  # [{type, strike, expiry, qty}]
    # Greeks
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0      # daily time decay
    vega: float = 0.0
    # Economics
    max_profit: float = 0.0
    max_loss: float = 0.0
    breakeven: float = 0.0
    cost: float = 0.0       # net premium paid/received
    # Metadata
    iv_at_entry: float = 0.0
    regime: str = "normal"
    rationale: str = ""
    ts: float = field(default_factory=time.time)


class OptionsEngine:
    """Automated options strategy selection and management.

    Strategy selection logic:
      - Trending + low IV: covered calls (sell premium, ride trend)
      - Trending + high IV: protective puts (hedge the long)
      - Range-bound + high IV: iron condor (sell premium both sides)
      - Pre-event + low IV: straddle (buy vol before catalyst)
      - IV > realized vol: sell vol (IV rich)
      - IV < realized vol: buy vol (IV cheap)
    """

    def __init__(self, bus: Bus):
        self.bus = bus
        self._regime: str = "normal"
        self._iv: dict[str, float] = {}           # asset -> current IV
        self._realized_vol: dict[str, float] = {}  # asset -> realized vol
        self._prices: dict[str, float] = {}
        self._strategies: list[OptionsStrategy] = []
        self._strategy_counter = 0
        self._stats = {"generated": 0, "active": 0}

        bus.subscribe("signal.options", self._on_options_signal)
        bus.subscribe("signal.regime", self._on_regime)
        bus.subscribe("market.snapshot", self._on_snapshot)

    async def _on_snapshot(self, snap: MarketSnapshot):
        self._prices.update(snap.prices)

    async def _on_regime(self, sig: Signal):
        if isinstance(sig, Signal):
            self._regime = sig.rationale.split(":")[-1].strip() if ":" in sig.rationale else "normal"

    async def _on_options_signal(self, sig: Signal):
        """Process options data signals and generate strategies."""
        if not isinstance(sig, Signal):
            return

        # Extract IV from options signal
        rationale = sig.rationale.lower()
        if "iv" in rationale:
            self._iv[sig.asset] = sig.strength  # normalized IV

        # Generate strategy based on conditions
        await self._evaluate_strategies(sig.asset)

    async def _evaluate_strategies(self, asset: str):
        """Evaluate and propose options strategies for an asset."""
        iv = self._iv.get(asset, 0.5)
        rv = self._realized_vol.get(asset, 0.4)
        price = self._prices.get(asset, 0)
        if price <= 0:
            return

        # Cooldown: don't spam strategies
        recent = [s for s in self._strategies if s.asset == asset and time.time() - s.ts < 300]
        if recent:
            return

        strategy = None

        if self._regime in ("trending",) and iv < 0.5:
            strategy = self._covered_call(asset, price, iv)
        elif self._regime in ("trending",) and iv >= 0.5:
            strategy = self._protective_put(asset, price, iv)
        elif self._regime in ("mean_reverting", "normal") and iv > 0.6:
            strategy = self._iron_condor(asset, price, iv)
        elif iv < rv - 0.1:
            strategy = self._straddle(asset, price, iv)
        elif iv > rv + 0.15:
            strategy = self._vol_sell(asset, price, iv)

        if strategy:
            self._strategies.append(strategy)
            if len(self._strategies) > 100:
                self._strategies = self._strategies[-50:]
            self._stats["generated"] += 1
            self._stats["active"] += 1

            log.info(
                "OPTIONS: %s %s on %s | cost=$%.2f max_profit=$%.2f max_loss=$%.2f | IV=%.2f regime=%s",
                strategy.strategy_id, strategy.name, asset,
                strategy.cost, strategy.max_profit, strategy.max_loss,
                iv, self._regime,
            )

            sig = Signal(
                agent_id="options_engine",
                asset=asset,
                direction="long" if strategy.direction == "bullish" else "short" if strategy.direction == "bearish" else "flat",
                strength=min(1.0, abs(strategy.max_profit / max(abs(strategy.max_loss), 1))),
                confidence=0.6,
                rationale=f"Options: {strategy.name} | {strategy.rationale}",
            )
            await self.bus.publish("signal.options_strategy", sig)
            await self.bus.publish("options.strategy", strategy)

    def _covered_call(self, asset: str, price: float, iv: float) -> OptionsStrategy:
        self._strategy_counter += 1
        strike = price * 1.05  # 5% OTM call
        premium = price * iv * 0.02  # simplified Black-Scholes approximation
        return OptionsStrategy(
            strategy_id=f"opt-{self._strategy_counter:04d}",
            name="covered_call",
            asset=asset, direction="bullish",
            legs=[
                {"type": "sell_call", "strike": round(strike, 2), "expiry": "30d", "qty": 1},
            ],
            delta=0.7, theta=-premium / 30, vega=-0.01,
            max_profit=round(premium + (strike - price), 2),
            max_loss=round(-price + premium, 2),
            breakeven=round(price - premium, 2),
            cost=round(-premium, 2),  # receive premium
            iv_at_entry=iv, regime=self._regime,
            rationale=f"Sell {strike:.0f} call for ${premium:.2f} premium, trending + low IV",
        )

    def _protective_put(self, asset: str, price: float, iv: float) -> OptionsStrategy:
        self._strategy_counter += 1
        strike = price * 0.95  # 5% OTM put
        premium = price * iv * 0.025
        return OptionsStrategy(
            strategy_id=f"opt-{self._strategy_counter:04d}",
            name="protective_put",
            asset=asset, direction="bullish",
            legs=[
                {"type": "buy_put", "strike": round(strike, 2), "expiry": "30d", "qty": 1},
            ],
            delta=0.85, theta=-premium / 30, vega=0.01,
            max_profit=float("inf"),
            max_loss=round(-(price - strike) - premium, 2),
            breakeven=round(price + premium, 2),
            cost=round(premium, 2),
            iv_at_entry=iv, regime=self._regime,
            rationale=f"Buy {strike:.0f} put for ${premium:.2f}, hedge downside in trending + high IV",
        )

    def _straddle(self, asset: str, price: float, iv: float) -> OptionsStrategy:
        self._strategy_counter += 1
        premium_call = price * iv * 0.02
        premium_put = price * iv * 0.02
        total_cost = premium_call + premium_put
        return OptionsStrategy(
            strategy_id=f"opt-{self._strategy_counter:04d}",
            name="straddle",
            asset=asset, direction="vol_long",
            legs=[
                {"type": "buy_call", "strike": round(price, 2), "expiry": "14d", "qty": 1},
                {"type": "buy_put", "strike": round(price, 2), "expiry": "14d", "qty": 1},
            ],
            delta=0.0, gamma=0.05, theta=-total_cost / 14, vega=0.02,
            max_profit=float("inf"),
            max_loss=round(-total_cost, 2),
            breakeven=round(price + total_cost, 2),
            cost=round(total_cost, 2),
            iv_at_entry=iv, regime=self._regime,
            rationale=f"Buy straddle for ${total_cost:.2f}, IV cheap vs realized vol",
        )

    def _iron_condor(self, asset: str, price: float, iv: float) -> OptionsStrategy:
        self._strategy_counter += 1
        call_strike = price * 1.08
        put_strike = price * 0.92
        premium = price * iv * 0.015
        return OptionsStrategy(
            strategy_id=f"opt-{self._strategy_counter:04d}",
            name="iron_condor",
            asset=asset, direction="neutral",
            legs=[
                {"type": "sell_call", "strike": round(call_strike, 2), "expiry": "30d", "qty": 1},
                {"type": "buy_call", "strike": round(call_strike * 1.03, 2), "expiry": "30d", "qty": 1},
                {"type": "sell_put", "strike": round(put_strike, 2), "expiry": "30d", "qty": 1},
                {"type": "buy_put", "strike": round(put_strike * 0.97, 2), "expiry": "30d", "qty": 1},
            ],
            delta=0.0, theta=-premium / 30, vega=-0.02,
            max_profit=round(premium, 2),
            max_loss=round(-(call_strike * 0.03 - premium), 2),
            breakeven=round(price, 2),
            cost=round(-premium, 2),
            iv_at_entry=iv, regime=self._regime,
            rationale=f"Iron condor [{put_strike:.0f}, {call_strike:.0f}] for ${premium:.2f}, range-bound + high IV",
        )

    def _vol_sell(self, asset: str, price: float, iv: float) -> OptionsStrategy:
        self._strategy_counter += 1
        premium = price * iv * 0.015
        return OptionsStrategy(
            strategy_id=f"opt-{self._strategy_counter:04d}",
            name="vol_sell",
            asset=asset, direction="neutral",
            legs=[
                {"type": "sell_call", "strike": round(price * 1.05, 2), "expiry": "14d", "qty": 1},
                {"type": "sell_put", "strike": round(price * 0.95, 2), "expiry": "14d", "qty": 1},
            ],
            delta=0.0, theta=-premium / 14, vega=-0.03,
            max_profit=round(premium, 2),
            max_loss=float("inf"),
            cost=round(-premium, 2),
            iv_at_entry=iv, regime=self._regime,
            rationale=f"Short strangle for ${premium:.2f}, IV rich vs realized",
        )

    def summary(self) -> dict:
        return {
            **self._stats,
            "regime": self._regime,
            "recent": [
                {
                    "id": s.strategy_id,
                    "name": s.name,
                    "asset": s.asset,
                    "cost": s.cost,
                    "max_profit": s.max_profit,
                    "iv": round(s.iv_at_entry, 3),
                }
                for s in self._strategies[-5:]
            ],
        }
