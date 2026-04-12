"""Genetic Algorithm Strategy Evolution — auto-generate trading strategies.

Inspired by Alpha Dawg (ELO-ranked specialist agents), Vault Royale
(AI strategies compete for AUM), TradeAlgoAVS (decentralized strategy
marketplace), and the broader trend of AI-generated trading strategies.

Uses genetic algorithms to evolve strategy configurations:
  1. Population: N random strategy configs (signal weights, risk params)
  2. Fitness: backtest each strategy, score by risk-adjusted returns
  3. Selection: top 20% survive to next generation
  4. Crossover: breed top strategies (mix signal weights)
  5. Mutation: random perturbation of parameters
  6. Repeat until convergence or generation limit

The best strategies get promoted to live trading (via StrategyNFTManager).

Bus integration:
  Publishes to: evolution.generation, evolution.champion, signal.evolved

No external dependencies — pure Python genetic algorithm.
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from copy import deepcopy

from .core import Bus, Signal

log = logging.getLogger("swarm.evolution")

# Signal sources that strategies can weight
SIGNAL_GENES = [
    "momentum", "mean_rev", "rsi", "macd", "bollinger",
    "vwap", "ichimoku", "whale", "news", "ml",
    "confluence", "funding_rate", "orderbook", "onchain",
    "fear_greed", "social", "smart_money", "correlation",
    "arbitrage", "fusion", "debate",
]

# Risk parameter ranges
RISK_GENES = {
    "max_position_pct": (2.0, 30.0),
    "stop_loss_pct": (1.0, 15.0),
    "take_profit_pct": (2.0, 30.0),
    "kelly_fraction": (0.05, 0.5),
    "min_confidence": (0.1, 0.8),
    "cooldown_minutes": (1.0, 60.0),
    "max_daily_trades": (5, 200),
}


@dataclass
class StrategyGenome:
    """A strategy configuration as a genome for genetic evolution."""
    genome_id: str
    generation: int
    # Signal weights (gene values 0-1)
    signal_weights: dict[str, float] = field(default_factory=dict)
    # Risk parameters
    risk_params: dict[str, float] = field(default_factory=dict)
    # Fitness metrics (from backtesting)
    fitness: float = 0.0
    sharpe_ratio: float = 0.0
    total_return: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    # Lineage
    parent_ids: list[str] = field(default_factory=list)
    mutations: int = 0

    @staticmethod
    def random(genome_id: str, generation: int = 0) -> "StrategyGenome":
        """Create a random strategy genome."""
        weights = {}
        for gene in SIGNAL_GENES:
            weights[gene] = round(random.uniform(0.0, 0.5), 3)
        # Normalize so weights sum to ~1
        total = sum(weights.values())
        if total > 0:
            weights = {k: round(v / total, 3) for k, v in weights.items()}

        risk = {}
        for param, (lo, hi) in RISK_GENES.items():
            if isinstance(lo, int):
                risk[param] = random.randint(int(lo), int(hi))
            else:
                risk[param] = round(random.uniform(lo, hi), 3)

        return StrategyGenome(
            genome_id=genome_id, generation=generation,
            signal_weights=weights, risk_params=risk,
        )


@dataclass
class GenerationReport:
    """Summary of one generation of evolution."""
    generation: int
    population_size: int
    best_fitness: float
    avg_fitness: float
    best_genome_id: str
    best_sharpe: float
    diversity: float        # measure of genetic diversity
    ts: float = field(default_factory=time.time)


class StrategyEvolution:
    """Genetic algorithm engine for evolving trading strategies.

    Parameters:
      population_size: number of strategies per generation
      elite_pct: top % that survive to next generation
      mutation_rate: probability of mutating each gene
      mutation_strength: magnitude of mutations
      crossover_rate: probability of crossover vs cloning
    """

    def __init__(self, bus: Bus, population_size: int = 50,
                 elite_pct: float = 0.2, mutation_rate: float = 0.15,
                 mutation_strength: float = 0.1, crossover_rate: float = 0.7,
                 max_generations: int = 100):
        self.bus = bus
        self.population_size = population_size
        self.elite_pct = elite_pct
        self.mutation_rate = mutation_rate
        self.mutation_strength = mutation_strength
        self.crossover_rate = crossover_rate
        self.max_generations = max_generations
        self._population: list[StrategyGenome] = []
        self._generation = 0
        self._genome_counter = 0
        self._history: list[GenerationReport] = []
        self._champion: StrategyGenome | None = None

    def _new_id(self) -> str:
        self._genome_counter += 1
        return f"gen{self._generation:03d}-{self._genome_counter:04d}"

    # ── Population Management ────────────────────────────────────

    def initialize_population(self):
        """Create initial random population."""
        self._population = []
        for _ in range(self.population_size):
            genome = StrategyGenome.random(self._new_id(), self._generation)
            self._population.append(genome)
        log.info("Evolution: initialized population of %d random strategies", self.population_size)

    def evaluate_fitness(self, genome: StrategyGenome, trade_results: list[dict]) -> float:
        """Evaluate strategy fitness from simulated trade results.

        Fitness = Sharpe ratio * sqrt(trade_count) * (1 - max_drawdown)
        This rewards: high risk-adjusted returns, many trades (statistical significance),
        and low drawdowns.
        """
        if not trade_results:
            genome.fitness = 0.0
            return 0.0

        returns = [t.get("pnl", 0) for t in trade_results]
        wins = sum(1 for r in returns if r > 0)
        genome.trade_count = len(returns)
        genome.win_rate = wins / max(len(returns), 1)

        genome.total_return = sum(returns)

        # Sharpe ratio
        if len(returns) >= 2:
            avg = sum(returns) / len(returns)
            var = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1)
            std = math.sqrt(max(var, 1e-12))
            genome.sharpe_ratio = (avg / std) * math.sqrt(252) if std > 0 else 0
        else:
            genome.sharpe_ratio = 0.0

        # Max drawdown
        peak = 0.0
        dd = 0.0
        equity = 0.0
        for r in returns:
            equity += r
            peak = max(peak, equity)
            dd = min(dd, equity - peak)
        genome.max_drawdown = abs(dd)

        # Composite fitness
        sharpe_component = max(0, genome.sharpe_ratio)
        trade_component = math.sqrt(max(genome.trade_count, 1))
        dd_penalty = 1 - min(genome.max_drawdown / max(abs(genome.total_return), 1), 1)

        genome.fitness = round(sharpe_component * trade_component * dd_penalty, 4)
        return genome.fitness

    # ── Genetic Operators ────────────────────────────────────────

    def select_parents(self) -> list[StrategyGenome]:
        """Select top performers for breeding (tournament selection)."""
        sorted_pop = sorted(self._population, key=lambda g: g.fitness, reverse=True)
        elite_count = max(2, int(len(sorted_pop) * self.elite_pct))
        return sorted_pop[:elite_count]

    def crossover(self, parent_a: StrategyGenome, parent_b: StrategyGenome) -> StrategyGenome:
        """Create offspring by mixing two parent genomes."""
        child = StrategyGenome(
            genome_id=self._new_id(),
            generation=self._generation,
            parent_ids=[parent_a.genome_id, parent_b.genome_id],
        )

        # Signal weight crossover: uniform crossover per gene
        for gene in SIGNAL_GENES:
            if random.random() < 0.5:
                child.signal_weights[gene] = parent_a.signal_weights.get(gene, 0.1)
            else:
                child.signal_weights[gene] = parent_b.signal_weights.get(gene, 0.1)

        # Risk parameter crossover: blend
        for param in RISK_GENES:
            alpha = random.uniform(0.3, 0.7)
            val_a = parent_a.risk_params.get(param, 0)
            val_b = parent_b.risk_params.get(param, 0)
            child.risk_params[param] = round(alpha * val_a + (1 - alpha) * val_b, 3)

        return child

    def mutate(self, genome: StrategyGenome) -> StrategyGenome:
        """Apply random mutations to a genome."""
        mutated = deepcopy(genome)
        mutated.genome_id = self._new_id()
        mutated.generation = self._generation

        # Mutate signal weights
        for gene in SIGNAL_GENES:
            if random.random() < self.mutation_rate:
                current = mutated.signal_weights.get(gene, 0.1)
                delta = random.gauss(0, self.mutation_strength)
                mutated.signal_weights[gene] = round(max(0, min(1, current + delta)), 3)
                mutated.mutations += 1

        # Mutate risk parameters
        for param, (lo, hi) in RISK_GENES.items():
            if random.random() < self.mutation_rate:
                current = mutated.risk_params.get(param, (lo + hi) / 2)
                range_size = hi - lo
                delta = random.gauss(0, range_size * self.mutation_strength)
                new_val = max(lo, min(hi, current + delta))
                if isinstance(lo, int):
                    new_val = int(new_val)
                else:
                    new_val = round(new_val, 3)
                mutated.risk_params[param] = new_val
                mutated.mutations += 1

        return mutated

    # ── Evolution Loop ───────────────────────────────────────────

    async def evolve_generation(self, fitness_fn=None):
        """Run one generation of evolution.

        Args:
            fitness_fn: Optional custom fitness function. If None, uses
                       random simulated results for development.
        """
        self._generation += 1

        if not self._population:
            self.initialize_population()

        # Evaluate fitness
        for genome in self._population:
            if fitness_fn:
                fitness_fn(genome)
            else:
                # Simulated fitness for development
                fake_trades = [
                    {"pnl": random.gauss(0, 10) * sum(genome.signal_weights.values())}
                    for _ in range(max(10, genome.risk_params.get("max_daily_trades", 50)))
                ]
                self.evaluate_fitness(genome, fake_trades)

        # Selection
        parents = self.select_parents()
        best = parents[0] if parents else None

        # Track champion
        if best and (not self._champion or best.fitness > self._champion.fitness):
            self._champion = deepcopy(best)

        # Report
        all_fitness = [g.fitness for g in self._population]
        avg_fitness = sum(all_fitness) / max(len(all_fitness), 1)

        # Diversity: std dev of fitness values
        if len(all_fitness) >= 2:
            mean_f = avg_fitness
            diversity = math.sqrt(sum((f - mean_f) ** 2 for f in all_fitness) / len(all_fitness))
        else:
            diversity = 0.0

        report = GenerationReport(
            generation=self._generation,
            population_size=len(self._population),
            best_fitness=best.fitness if best else 0.0,
            avg_fitness=round(avg_fitness, 4),
            best_genome_id=best.genome_id if best else "",
            best_sharpe=best.sharpe_ratio if best else 0.0,
            diversity=round(diversity, 4),
        )
        self._history.append(report)

        log.info(
            "EVOLUTION gen=%d best=%.4f avg=%.4f sharpe=%.2f diversity=%.4f champion=%s",
            self._generation, report.best_fitness, report.avg_fitness,
            report.best_sharpe, report.diversity,
            best.genome_id if best else "none",
        )

        # Create next generation
        new_population: list[StrategyGenome] = []

        # Elitism: top performers survive unchanged
        for elite in parents[:max(2, int(self.population_size * 0.1))]:
            clone = deepcopy(elite)
            clone.genome_id = self._new_id()
            clone.generation = self._generation
            new_population.append(clone)

        # Fill remaining slots with crossover + mutation
        while len(new_population) < self.population_size:
            if len(parents) >= 2 and random.random() < self.crossover_rate:
                p1, p2 = random.sample(parents, 2)
                child = self.crossover(p1, p2)
            else:
                parent = random.choice(parents) if parents else StrategyGenome.random(self._new_id())
                child = deepcopy(parent)
                child.genome_id = self._new_id()
                child.generation = self._generation

            child = self.mutate(child)
            new_population.append(child)

        self._population = new_population

        await self.bus.publish("evolution.generation", report)
        if best:
            await self.bus.publish("evolution.champion", {
                "genome": best,
                "generation": self._generation,
            })

        return report

    async def run_evolution(self, generations: int = 10, fitness_fn=None):
        """Run multiple generations of evolution."""
        self.initialize_population()
        for _ in range(generations):
            await self.evolve_generation(fitness_fn)
            if self._generation >= self.max_generations:
                break
        return self._champion

    def get_champion(self) -> StrategyGenome | None:
        """Get the best strategy found so far."""
        return self._champion

    def summary(self) -> dict:
        return {
            "generation": self._generation,
            "population_size": len(self._population),
            "champion": {
                "id": self._champion.genome_id if self._champion else None,
                "fitness": self._champion.fitness if self._champion else 0,
                "sharpe": self._champion.sharpe_ratio if self._champion else 0,
                "return": self._champion.total_return if self._champion else 0,
                "top_signals": sorted(
                    self._champion.signal_weights.items(),
                    key=lambda x: x[1], reverse=True,
                )[:5] if self._champion else [],
            } if self._champion else None,
            "history": [
                {
                    "gen": h.generation,
                    "best": round(h.best_fitness, 4),
                    "avg": round(h.avg_fitness, 4),
                    "diversity": round(h.diversity, 4),
                }
                for h in self._history[-10:]
            ],
        }
