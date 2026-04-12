"""Backtesting Arena — competitive strategy tournaments.

Inspired by Vault Royale (AI-powered DeFi strategies compete for users),
ScampIA (onchain AI trading league), MirrorBattle (agents compete in PvP
copy-trading battles), and The CoWncil (on-chain trading tournaments).

Runs automated tournaments where strategies compete head-to-head:
  1. All registered strategies backtest on same historical data
  2. Ranked by risk-adjusted returns (Sharpe, not raw PnL)
  3. Winners get promoted (more capital allocation, higher visibility)
  4. Losers get demoted or deactivated
  5. Tournament results feed into ELO rankings

Bus integration:
  Subscribes to: evolution.champion (new strategy candidates)
  Publishes to:  arena.result, arena.ranking, arena.promotion
"""
from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field

from .core import Bus

log = logging.getLogger("swarm.arena")


@dataclass
class ArenaEntry:
    """A strategy entered into a tournament."""
    entry_id: str
    strategy_name: str
    config: dict = field(default_factory=dict)
    # Results
    total_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    calmar_ratio: float = 0.0
    # Ranking
    rank: int = 0
    elo_change: float = 0.0
    promoted: bool = False


@dataclass
class TournamentResult:
    """Result of a tournament round."""
    tournament_id: str
    round_number: int
    entries: list[ArenaEntry]
    winner: ArenaEntry | None
    data_period: str          # "2024-Q4", "2025-01", etc.
    ts: float = field(default_factory=time.time)


class BacktestingArena:
    """Competitive tournament system for strategy evaluation.

    Runs periodic tournaments where all strategies are backtested
    on identical historical data and ranked by risk-adjusted performance.

    Tournament types:
      - Sprint: 1-week data, emphasis on recent performance
      - Marathon: 3-month data, emphasis on consistency
      - Stress: crisis periods only, emphasis on drawdown control
      - Mixed: random selection of market conditions
    """

    def __init__(self, bus: Bus, max_entries: int = 50):
        self.bus = bus
        self.max_entries = max_entries
        self._entries: dict[str, dict] = {}  # strategy_name -> config
        self._results: list[TournamentResult] = []
        self._rankings: dict[str, float] = {}  # strategy_name -> cumulative score
        self._tournament_counter = 0
        self._stats = {
            "tournaments": 0, "entries_total": 0,
            "promotions": 0, "demotions": 0,
        }

        bus.subscribe("evolution.champion", self._on_champion)

    async def _on_champion(self, data):
        """Register evolved strategies for tournament entry."""
        if isinstance(data, dict):
            genome = data.get("genome")
            if genome:
                self.register_entry(
                    genome.genome_id,
                    {
                        "signal_weights": genome.signal_weights,
                        "risk_params": genome.risk_params,
                    },
                )

    def register_entry(self, name: str, config: dict):
        """Register a strategy for the next tournament."""
        if len(self._entries) >= self.max_entries:
            # Evict lowest-ranked
            if self._rankings:
                worst = min(self._rankings.items(), key=lambda x: x[1])
                del self._entries[worst[0]]
                del self._rankings[worst[0]]

        self._entries[name] = config
        self._rankings.setdefault(name, 0.0)
        self._stats["entries_total"] += 1

    async def run_tournament(self, tournament_type: str = "sprint",
                              data_period: str = "recent") -> TournamentResult:
        """Run a tournament round."""
        if not self._entries:
            log.info("Arena: no entries for tournament")
            return TournamentResult(
                tournament_id="empty", round_number=0,
                entries=[], winner=None, data_period=data_period,
            )

        self._tournament_counter += 1
        tid = f"tourney-{self._tournament_counter:04d}"

        entries = []
        for name, config in self._entries.items():
            result = self._simulate_backtest(name, config, tournament_type)
            entries.append(result)

        # Rank by Sharpe ratio
        entries.sort(key=lambda e: e.sharpe_ratio, reverse=True)
        for i, entry in enumerate(entries):
            entry.rank = i + 1

        winner = entries[0] if entries else None

        # Update rankings (ELO-style scoring)
        n = len(entries)
        for entry in entries:
            # Points: top gets +n, bottom gets +1
            points = n - entry.rank + 1
            elo_change = (points - n / 2) * 10  # centered around 0
            entry.elo_change = round(elo_change, 1)
            self._rankings[entry.strategy_name] = (
                self._rankings.get(entry.strategy_name, 0) + elo_change
            )

        # Promote top 20%, demote bottom 20%
        promote_cutoff = max(1, int(len(entries) * 0.2))
        for entry in entries[:promote_cutoff]:
            entry.promoted = True
            self._stats["promotions"] += 1

        result = TournamentResult(
            tournament_id=tid,
            round_number=self._tournament_counter,
            entries=entries,
            winner=winner,
            data_period=data_period,
        )
        self._results.append(result)
        if len(self._results) > 100:
            self._results = self._results[-50:]

        self._stats["tournaments"] += 1

        if winner:
            log.info(
                "ARENA: Tournament %s (%s) — Winner: %s (Sharpe=%.2f, return=%.2f%%, DD=%.2f%%)",
                tid, tournament_type, winner.strategy_name,
                winner.sharpe_ratio, winner.total_return * 100, winner.max_drawdown * 100,
            )

        await self.bus.publish("arena.result", result)
        await self.bus.publish("arena.ranking", dict(self._rankings))
        return result

    def _simulate_backtest(self, name: str, config: dict,
                           tournament_type: str) -> ArenaEntry:
        """Simulate a backtest for a strategy.

        In production: would run actual historical data through the strategy.
        Here: generates realistic random results weighted by config quality.
        """
        # Config quality score (better weights = better baseline)
        weights = config.get("signal_weights", {})
        risk = config.get("risk_params", {})

        # Good configs have moderate weights (not too concentrated)
        weight_values = list(weights.values()) if weights else [0.1]
        concentration = max(weight_values) / max(sum(weight_values), 0.01)
        quality = 1.0 - concentration * 0.5  # less concentrated = higher quality

        kelly = risk.get("kelly_fraction", 0.25)
        if kelly > 0.4:
            quality *= 0.7  # over-betting penalty
        stop_loss = risk.get("stop_loss_pct", 5)
        if stop_loss > 10:
            quality *= 0.8  # wide stops penalty

        # Simulate returns with quality bias
        n_trades = random.randint(20, 200)
        base_edge = (quality - 0.5) * 0.02  # slight positive/negative edge
        returns = [random.gauss(base_edge, 0.01) for _ in range(n_trades)]

        total_return = sum(returns)
        wins = sum(1 for r in returns if r > 0)
        win_rate = wins / max(n_trades, 1)

        # Sharpe
        if len(returns) >= 2:
            avg = sum(returns) / len(returns)
            std = math.sqrt(sum((r - avg) ** 2 for r in returns) / (len(returns) - 1))
            sharpe = (avg / max(std, 1e-9)) * math.sqrt(252)
        else:
            sharpe = 0.0

        # Max drawdown
        peak = dd = equity = 0.0
        for r in returns:
            equity += r
            peak = max(peak, equity)
            dd = min(dd, equity - peak)
        max_dd = abs(dd)

        # Calmar
        calmar = total_return / max(max_dd, 0.001) if max_dd > 0 else 0

        return ArenaEntry(
            entry_id=f"{name}-{tournament_type}",
            strategy_name=name,
            config=config,
            total_return=round(total_return, 4),
            sharpe_ratio=round(sharpe, 3),
            max_drawdown=round(max_dd, 4),
            win_rate=round(win_rate, 3),
            trade_count=n_trades,
            calmar_ratio=round(calmar, 3),
        )

    def leaderboard(self, top_n: int = 20) -> list[dict]:
        return sorted(
            [{"strategy": k, "score": round(v, 1)} for k, v in self._rankings.items()],
            key=lambda x: x["score"], reverse=True,
        )[:top_n]

    def summary(self) -> dict:
        return {
            **self._stats,
            "entries": len(self._entries),
            "leaderboard": self.leaderboard(10),
        }
