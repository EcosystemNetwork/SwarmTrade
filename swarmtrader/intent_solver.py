"""Intent Solver Network — competitive intent-based execution.

Inspired by Nimble (AI agent solver network), Sui-Intent (intent-based
trading with Dutch Auction), IntentSwap (solvers compete in real-time),
and Grimoire (language for expressing financial intent).

Instead of submitting orders directly, agents express intents:
  "Buy $500 of ETH at best available price within 2% slippage"

Multiple solver agents compete to fill the intent:
  1. Solver A quotes via Uniswap: $499.50 of ETH
  2. Solver B quotes via Aerodrome: $499.80 of ETH
  3. Solver C quotes via cross-DEX routing: $499.95 of ETH
  → Solver C wins, executes, earns fee

This creates a competitive market for execution quality.

Bus integration:
  Subscribes to: intent.new (intercepts for solver competition)
  Publishes to:  intent.solved, intent.quote, signal.solver

No external dependencies.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .core import Bus, TradeIntent, ExecutionReport

log = logging.getLogger("swarm.solver")


@dataclass
class SolverQuote:
    """A solver's quote for filling an intent."""
    solver_id: str
    intent_id: str
    venue: str              # which DEX/exchange
    route: str              # routing path description
    amount_out: float       # how much the user gets
    price: float            # effective price
    slippage_bps: float     # estimated slippage
    gas_cost_usd: float
    fee_usd: float          # solver's fee
    expires_at: float       # quote validity
    ts: float = field(default_factory=time.time)

    @property
    def net_out(self) -> float:
        """Net amount after fees and gas."""
        return self.amount_out - self.fee_usd - self.gas_cost_usd


@dataclass
class SolverResult:
    """Result of a solver competition for an intent."""
    intent_id: str
    winner: SolverQuote | None
    all_quotes: list[SolverQuote]
    improvement_bps: float    # how much better than worst quote
    ts: float = field(default_factory=time.time)


class Solver:
    """A solver agent that quotes execution for intents."""

    def __init__(self, solver_id: str, venue: str, fee_bps: float = 5.0,
                 base_slippage_bps: float = 10.0):
        self.solver_id = solver_id
        self.venue = venue
        self.fee_bps = fee_bps
        self.base_slippage_bps = base_slippage_bps
        self.fills = 0
        self.total_volume = 0.0

    def quote(self, intent: TradeIntent, market_price: float) -> SolverQuote | None:
        """Generate a quote for an intent."""
        if market_price <= 0:
            return None

        # Calculate slippage based on size + venue characteristics
        size_factor = intent.amount_in / 10_000  # larger orders = more slippage
        slippage_bps = self.base_slippage_bps * (1 + size_factor)
        slip = slippage_bps / 10_000

        # Effective price after slippage
        from .core import QUOTE_ASSETS
        buying = intent.asset_in.upper() in QUOTE_ASSETS
        if buying:
            eff_price = market_price * (1 + slip)
            amount_out = intent.amount_in / eff_price
        else:
            eff_price = market_price * (1 - slip)
            amount_out = intent.amount_in * eff_price

        fee = intent.amount_in * (self.fee_bps / 10_000)
        gas = 0.5  # estimated gas in USD

        return SolverQuote(
            solver_id=self.solver_id,
            intent_id=intent.id,
            venue=self.venue,
            route=f"{intent.asset_in}->{intent.asset_out} via {self.venue}",
            amount_out=round(amount_out, 6),
            price=round(eff_price, 4),
            slippage_bps=round(slippage_bps, 1),
            gas_cost_usd=gas,
            fee_usd=round(fee, 4),
            expires_at=time.time() + 30,
        )


class IntentSolverNetwork:
    """Competitive network of solvers competing for best execution.

    Default solvers simulate different DEX venues with varying
    characteristics. External solvers can register via the Gateway.
    """

    def __init__(self, bus: Bus, auction_window_s: float = 5.0):
        self.bus = bus
        self.auction_window_s = auction_window_s
        self._solvers: list[Solver] = []
        self._results: list[SolverResult] = []
        self._market_prices: dict[str, float] = {}
        self._stats = {
            "intents_received": 0, "solved": 0,
            "avg_improvement_bps": 0.0, "total_volume": 0.0,
        }

        # Register default solvers (simulate different DEXes)
        self._solvers = [
            Solver("uniswap_v3", "Uniswap V3", fee_bps=5, base_slippage_bps=8),
            Solver("aerodrome", "Aerodrome", fee_bps=3, base_slippage_bps=12),
            Solver("sushiswap", "SushiSwap", fee_bps=6, base_slippage_bps=15),
            Solver("1inch", "1inch Aggregator", fee_bps=2, base_slippage_bps=5),
            Solver("cross_dex", "Cross-DEX Router", fee_bps=4, base_slippage_bps=3),
        ]

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("intent.new", self._on_intent)

    def register_solver(self, solver: Solver):
        """Register an external solver."""
        self._solvers.append(solver)
        log.info("Solver registered: %s (%s)", solver.solver_id, solver.venue)

    async def _on_snapshot(self, snap):
        """Update market prices for quoting."""
        if hasattr(snap, "prices"):
            self._market_prices.update(snap.prices)

    async def _on_intent(self, intent: TradeIntent):
        """Run solver competition for an incoming intent."""
        self._stats["intents_received"] += 1

        # Get market price for the base asset
        from .core import QUOTE_ASSETS
        base = intent.asset_out if intent.asset_in.upper() in QUOTE_ASSETS else intent.asset_in
        price = self._market_prices.get(base, 0)
        if price <= 0:
            return

        # Collect quotes from all solvers
        quotes = []
        for solver in self._solvers:
            quote = solver.quote(intent, price)
            if quote:
                quotes.append(quote)

        if not quotes:
            return

        # Select winner (highest net_out for buyer, highest net_out for seller)
        quotes.sort(key=lambda q: q.net_out, reverse=True)
        winner = quotes[0]
        worst = quotes[-1]

        # Calculate improvement
        improvement_bps = 0.0
        if worst.net_out > 0:
            improvement_bps = ((winner.net_out - worst.net_out) / worst.net_out) * 10_000

        result = SolverResult(
            intent_id=intent.id,
            winner=winner,
            all_quotes=quotes,
            improvement_bps=round(improvement_bps, 1),
        )
        self._results.append(result)
        if len(self._results) > 500:
            self._results = self._results[-250:]

        # Update solver stats
        for solver in self._solvers:
            if solver.solver_id == winner.solver_id:
                solver.fills += 1
                solver.total_volume += intent.amount_in

        self._stats["solved"] += 1
        self._stats["total_volume"] += intent.amount_in
        # Running average improvement
        n = self._stats["solved"]
        self._stats["avg_improvement_bps"] = (
            (self._stats["avg_improvement_bps"] * (n - 1) + improvement_bps) / n
        )

        log.info(
            "SOLVER: %s won for %s (%s) | out=%.4f via %s | "
            "improvement=%.1fbps over %d quotes",
            winner.solver_id, intent.id, base,
            winner.net_out, winner.venue,
            improvement_bps, len(quotes),
        )

        await self.bus.publish("intent.solved", result)

    def solver_leaderboard(self) -> list[dict]:
        return sorted(
            [
                {
                    "solver_id": s.solver_id,
                    "venue": s.venue,
                    "fills": s.fills,
                    "volume": round(s.total_volume, 2),
                    "fee_bps": s.fee_bps,
                }
                for s in self._solvers
            ],
            key=lambda x: x["fills"], reverse=True,
        )

    def summary(self) -> dict:
        return {
            **self._stats,
            "solvers": len(self._solvers),
            "leaderboard": self.solver_leaderboard(),
        }
