"""Backtesting Arena — competitive strategy tournaments.

Runs automated tournaments where strategies compete head-to-head using
the real BacktestEngine against historical CoinGecko OHLCV data:
  1. All registered strategies backtest on same historical candles
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
import time
from dataclasses import dataclass, field

from .core import Bus

log = logging.getLogger("swarm.arena")

# Import real backtester for tournament evaluation
try:
    from .backtester import BacktestEngine, HistoricalDataFetcher
    from .strategy_evolution import _GenomeBacktestStrategy, StrategyGenome
    _HAS_BACKTESTER = True
except ImportError:
    _HAS_BACKTESTER = False


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

    def __init__(self, bus: Bus, max_entries: int = 50,
                 candles: list | None = None):
        self.bus = bus
        self.max_entries = max_entries
        self._candles = candles  # historical OHLCV for real backtesting
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
                              data_period: str = "recent",
                              asset: str = "ETH") -> TournamentResult:
        """Run a tournament round.

        Uses the real BacktestEngine to evaluate each strategy against
        historical candle data.  Fetches candles from CoinGecko on first
        call if none were provided at init.
        """
        if not self._entries:
            log.info("Arena: no entries for tournament")
            return TournamentResult(
                tournament_id="empty", round_number=0,
                entries=[], winner=None, data_period=data_period,
            )

        # Ensure we have candle data
        if not self._candles and _HAS_BACKTESTER:
            log.info("Arena: fetching historical candles for %s ...", asset)
            fetcher = HistoricalDataFetcher()
            self._candles = await fetcher.fetch_ohlcv(asset, days=365)
            await fetcher.close()

        if not self._candles or not _HAS_BACKTESTER:
            log.warning("Arena: no candle data available, cannot run tournament")
            return TournamentResult(
                tournament_id="empty", round_number=0,
                entries=[], winner=None, data_period=data_period,
            )

        self._tournament_counter += 1
        tid = f"tourney-{self._tournament_counter:04d}"

        entries = []
        for name, config in self._entries.items():
            result = self._run_real_backtest(name, config, tournament_type)
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

    def _run_real_backtest(self, name: str, config: dict,
                           tournament_type: str) -> ArenaEntry:
        """Run a strategy config through the real BacktestEngine against
        historical candle data.  Returns an ArenaEntry with actual metrics."""
        # Build a StrategyGenome from the config dict
        genome = StrategyGenome(
            genome_id=name, generation=0,
            signal_weights=config.get("signal_weights", {}),
            risk_params=config.get("risk_params", {}),
        )
        strategy = _GenomeBacktestStrategy(genome)

        # Select candle slice based on tournament type
        candles = self._candles
        if tournament_type == "sprint" and len(candles) > 30:
            candles = candles[-30:]       # ~1 month
        elif tournament_type == "stress" and len(candles) > 90:
            # Use the most volatile 90-day window (highest range/close ratio)
            best_start, best_vol = 0, 0.0
            for i in range(0, len(candles) - 90, 7):
                window = candles[i:i + 90]
                vol = sum((c.high - c.low) / max(c.close, 1) for c in window)
                if vol > best_vol:
                    best_vol = vol
                    best_start = i
            candles = candles[best_start:best_start + 90]
        elif tournament_type == "marathon" and len(candles) > 90:
            candles = candles[-90:]       # ~3 months
        # "mixed" uses full dataset

        engine = BacktestEngine(fee_rate=0.001, slippage_bps=5.0,
                                initial_capital=10_000.0)
        trade_pct = genome.risk_params.get("max_position_pct", 10.0) / 100
        result = engine.run(strategy, candles, trade_size_pct=trade_pct)

        return ArenaEntry(
            entry_id=f"{name}-{tournament_type}",
            strategy_name=name,
            config=config,
            total_return=round(result.total_return_pct / 100, 4),
            sharpe_ratio=round(result.sharpe_ratio, 3),
            max_drawdown=round(result.max_drawdown_pct / 100, 4),
            win_rate=round(result.win_rate, 3),
            trade_count=result.num_trades,
            calmar_ratio=round(result.calmar_ratio, 3),
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
