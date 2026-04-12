"""Strategy Backtesting Framework — Research, Backtest, Implement (RBI).

Inspired by Moon Dev's Harvard-Algorithmic-Trading-with-AI methodology.
Validates trading strategies against historical data before deploying capital.

Features:
  - Walk-forward backtesting with out-of-sample validation
  - Multiple strategy support (momentum, mean-reversion, breakout, DCA)
  - Monte Carlo simulation for confidence intervals
  - Risk metrics: Sharpe, Sortino, max drawdown, Calmar ratio
  - Fee-aware PnL calculation (configurable exchange fees)
  - Integration with swarm Bus for live strategy selection

Data sources:
  - CoinGecko historical OHLCV (free, no key needed)
  - Hyperliquid candles
  - Local CSV/JSON data files

Publishes:
  backtest.result          — completed backtest results
  strategy.validated       — strategy passed validation threshold
  intelligence.backtest    — backtest metrics for dashboard

Environment variables:
  BACKTEST_DATA_DIR        — directory for cached OHLCV data (default: ./backtest_data)
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import os
import random
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import aiohttp

from .core import Bus, Signal

log = logging.getLogger("swarm.backtester")


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------
@dataclass
class OHLCV:
    """Single candlestick."""
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class BacktestTrade:
    """A simulated trade."""
    ts: float
    side: str          # "buy" or "sell"
    price: float
    quantity: float
    fee: float
    pnl: float = 0.0   # realized PnL (set on close)
    reason: str = ""


@dataclass
class BacktestResult:
    """Complete backtest output."""
    strategy_name: str
    asset: str
    period_start: float
    period_end: float
    trades: list[BacktestTrade]
    # Performance metrics
    total_pnl: float = 0.0
    total_return_pct: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_drawdown_pct: float = 0.0
    calmar_ratio: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_trade_pnl: float = 0.0
    total_fees: float = 0.0
    # Walk-forward
    in_sample_sharpe: float = 0.0
    out_sample_sharpe: float = 0.0
    # Monte Carlo
    mc_median_pnl: float = 0.0
    mc_5th_percentile: float = 0.0
    mc_95th_percentile: float = 0.0
    # Meta
    num_trades: int = 0
    holding_time_avg: float = 0.0
    exposure_pct: float = 0.0


# ---------------------------------------------------------------------------
# Historical Data Fetcher
# ---------------------------------------------------------------------------
class HistoricalDataFetcher:
    """Fetch and cache historical OHLCV data from CoinGecko."""

    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = Path(cache_dir or os.getenv("BACKTEST_DATA_DIR", "./backtest_data"))
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def fetch_ohlcv(self, asset: str, days: int = 365,
                          interval: str = "daily") -> list[OHLCV]:
        """Fetch OHLCV from CoinGecko (free tier, no key).

        Combines /ohlc (open/high/low/close) with /market_chart
        (total_volumes) to produce candles with real volume data.
        """
        # Check cache first
        cache_file = self.cache_dir / f"{asset.lower()}_{days}d_{interval}.json"
        if cache_file.exists():
            age_hours = (time.time() - cache_file.stat().st_mtime) / 3600
            if age_hours < 24:  # Cache valid for 24h
                return self._load_cache(cache_file)

        # CoinGecko asset ID mapping
        cg_ids = {
            "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
            "AVAX": "avalanche-2", "ARB": "arbitrum", "OP": "optimism",
            "LINK": "chainlink", "DOT": "polkadot", "ADA": "cardano",
            "XRP": "ripple", "DOGE": "dogecoin", "MATIC": "matic-network",
            "ATOM": "cosmos", "NEAR": "near", "SUI": "sui",
            "APT": "aptos", "SEI": "sei-network", "TIA": "celestia",
            "INJ": "injective-protocol", "JUP": "jupiter-exchange-solana",
            "PEPE": "pepe", "WIF": "dogwifcoin", "BONK": "bonk",
            "RENDER": "render-token", "FET": "artificial-superintelligence-alliance",
        }

        cg_id = cg_ids.get(asset.upper(), asset.lower())

        # Use /market_chart for price+volume, then /ohlc for OHLC structure
        session = await self._get_session()

        # Step 1: Fetch volume data from /market_chart (includes total_volumes)
        volume_by_day: dict[int, float] = {}
        try:
            mc_url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart"
            mc_params = {"vs_currency": "usd", "days": str(days), "interval": "daily"}
            async with session.get(mc_url, params=mc_params) as resp:
                if resp.status == 200:
                    mc_data = await resp.json()
                    for ts_ms, vol in mc_data.get("total_volumes", []):
                        # Key by day (truncate to midnight)
                        day_key = int(ts_ms / 1000) // 86400
                        volume_by_day[day_key] = vol
        except Exception as e:
            log.debug("CoinGecko market_chart volume fetch: %s", e)

        # Step 2: Fetch OHLC candles
        url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc"
        params = {"vs_currency": "usd", "days": str(days)}
        try:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    raw = await resp.json()
                    candles = []
                    for c in raw:
                        if len(c) >= 5:
                            ts_sec = c[0] / 1000
                            day_key = int(ts_sec) // 86400
                            candles.append(OHLCV(
                                ts=ts_sec,
                                open=c[1], high=c[2],
                                low=c[3], close=c[4],
                                volume=volume_by_day.get(day_key, 0.0),
                            ))
                    if candles:
                        self._save_cache(cache_file, candles)
                    log.info("Fetched %d candles for %s (%dd, %d with volume)",
                             len(candles), asset, days,
                             sum(1 for c in candles if c.volume > 0))
                    return candles
                elif resp.status == 429:
                    log.warning("CoinGecko rate limited, using cache")
                    if cache_file.exists():
                        return self._load_cache(cache_file)
                else:
                    log.warning("CoinGecko %d for %s", resp.status, asset)
        except Exception as e:
            log.warning("CoinGecko fetch error: %s", e)

        # Fallback to cache even if stale
        if cache_file.exists():
            return self._load_cache(cache_file)
        return []

    def _save_cache(self, path: Path, candles: list[OHLCV]):
        data = [{"ts": c.ts, "o": c.open, "h": c.high,
                 "l": c.low, "c": c.close, "v": c.volume} for c in candles]
        path.write_text(json.dumps(data))

    def _load_cache(self, path: Path) -> list[OHLCV]:
        data = json.loads(path.read_text())
        return [OHLCV(ts=d["ts"], open=d["o"], high=d["h"],
                       low=d["l"], close=d["c"], volume=d["v"]) for d in data]

    def load_csv(self, path: str) -> list[OHLCV]:
        """Load OHLCV from CSV file (ts,open,high,low,close,volume)."""
        candles = []
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                candles.append(OHLCV(
                    ts=float(row.get("ts", row.get("timestamp", 0))),
                    open=float(row.get("open", 0)),
                    high=float(row.get("high", 0)),
                    low=float(row.get("low", 0)),
                    close=float(row.get("close", 0)),
                    volume=float(row.get("volume", 0)),
                ))
        return candles


# ---------------------------------------------------------------------------
# Built-in Strategies
# ---------------------------------------------------------------------------
class BacktestStrategy:
    """Base class for backtestable strategies."""
    name: str = "base"

    def on_candle(self, candle: OHLCV, history: list[OHLCV],
                  position: float) -> str | None:
        """Return 'buy', 'sell', or None."""
        return None


class MomentumStrategy(BacktestStrategy):
    """Dual SMA momentum crossover."""
    name = "momentum_sma"

    def __init__(self, fast: int = 10, slow: int = 30):
        self.fast = fast
        self.slow = slow

    def on_candle(self, candle: OHLCV, history: list[OHLCV],
                  position: float) -> str | None:
        if len(history) < self.slow:
            return None
        closes = [c.close for c in history[-self.slow:]]
        fast_ma = sum(closes[-self.fast:]) / self.fast
        slow_ma = sum(closes) / self.slow

        if fast_ma > slow_ma and position <= 0:
            return "buy"
        elif fast_ma < slow_ma and position > 0:
            return "sell"
        return None


class MeanReversionStrategy(BacktestStrategy):
    """Bollinger Band mean reversion."""
    name = "mean_reversion_bb"

    def __init__(self, period: int = 20, std_mult: float = 2.0):
        self.period = period
        self.std_mult = std_mult

    def on_candle(self, candle: OHLCV, history: list[OHLCV],
                  position: float) -> str | None:
        if len(history) < self.period:
            return None
        closes = [c.close for c in history[-self.period:]]
        mean = sum(closes) / self.period
        variance = sum((c - mean) ** 2 for c in closes) / self.period
        std = variance ** 0.5
        upper = mean + self.std_mult * std
        lower = mean - self.std_mult * std

        if candle.close < lower and position <= 0:
            return "buy"
        elif candle.close > upper and position > 0:
            return "sell"
        return None


class RSIMomentumStrategy(BacktestStrategy):
    """RSI-based strategy with overbought/oversold thresholds."""
    name = "rsi_momentum"

    def __init__(self, period: int = 14, oversold: float = 30,
                 overbought: float = 70):
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def on_candle(self, candle: OHLCV, history: list[OHLCV],
                  position: float) -> str | None:
        if len(history) < self.period + 1:
            return None

        closes = [c.close for c in history[-(self.period + 1):]]
        gains = []
        losses = []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(delta, 0))
            losses.append(max(-delta, 0))

        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0

        if avg_loss == 0:
            rsi = 100
        else:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))

        if rsi < self.oversold and position <= 0:
            return "buy"
        elif rsi > self.overbought and position > 0:
            return "sell"
        return None


class DCAStrategy(BacktestStrategy):
    """Dollar Cost Averaging — buy at regular intervals."""
    name = "dca"

    def __init__(self, buy_every_n: int = 7):
        self.buy_every_n = buy_every_n
        self._counter = 0

    def on_candle(self, candle: OHLCV, history: list[OHLCV],
                  position: float) -> str | None:
        self._counter += 1
        if self._counter >= self.buy_every_n:
            self._counter = 0
            return "buy"
        return None


class BreakoutStrategy(BacktestStrategy):
    """Donchian channel breakout."""
    name = "breakout_donchian"

    def __init__(self, period: int = 20):
        self.period = period

    def on_candle(self, candle: OHLCV, history: list[OHLCV],
                  position: float) -> str | None:
        if len(history) < self.period:
            return None
        highs = [c.high for c in history[-self.period:]]
        lows = [c.low for c in history[-self.period:]]
        upper = max(highs)
        lower = min(lows)

        if candle.close > upper and position <= 0:
            return "buy"
        elif candle.close < lower and position > 0:
            return "sell"
        return None


# All available strategies
STRATEGIES: dict[str, Callable[[], BacktestStrategy]] = {
    "momentum_sma": lambda: MomentumStrategy(),
    "momentum_sma_fast": lambda: MomentumStrategy(fast=5, slow=15),
    "mean_reversion_bb": lambda: MeanReversionStrategy(),
    "mean_reversion_tight": lambda: MeanReversionStrategy(period=14, std_mult=1.5),
    "rsi_momentum": lambda: RSIMomentumStrategy(),
    "rsi_aggressive": lambda: RSIMomentumStrategy(period=7, oversold=25, overbought=75),
    "dca_weekly": lambda: DCAStrategy(buy_every_n=7),
    "dca_daily": lambda: DCAStrategy(buy_every_n=1),
    "breakout_donchian": lambda: BreakoutStrategy(),
    "breakout_short": lambda: BreakoutStrategy(period=10),
}


# ---------------------------------------------------------------------------
# Backtesting Engine
# ---------------------------------------------------------------------------
class BacktestEngine:
    """Run strategies against historical data with realistic simulation."""

    def __init__(self, fee_rate: float = 0.001, slippage_bps: float = 5.0,
                 initial_capital: float = 10000.0):
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.initial_capital = initial_capital

    def run(self, strategy: BacktestStrategy, candles: list[OHLCV],
            trade_size_pct: float = 0.1) -> BacktestResult:
        """Run a single backtest."""
        if len(candles) < 10:
            return BacktestResult(
                strategy_name=strategy.name,
                asset="unknown",
                period_start=0, period_end=0,
                trades=[],
            )

        capital = self.initial_capital
        position = 0.0       # units held
        entry_price = 0.0
        trades: list[BacktestTrade] = []
        equity_curve: list[float] = []
        daily_returns: list[float] = []
        history: list[OHLCV] = []
        total_fees = 0.0
        time_in_position = 0
        total_candles = len(candles)

        for candle in candles:
            history.append(candle)
            action = strategy.on_candle(candle, history, position)

            if action == "buy" and position <= 0:
                # Apply slippage
                fill_price = candle.close * (1 + self.slippage_bps / 10000)
                trade_usd = capital * trade_size_pct
                quantity = trade_usd / fill_price
                fee = trade_usd * self.fee_rate
                capital -= (trade_usd + fee)
                position += quantity
                entry_price = fill_price
                total_fees += fee
                trades.append(BacktestTrade(
                    ts=candle.ts, side="buy", price=fill_price,
                    quantity=quantity, fee=fee, reason=strategy.name,
                ))

            elif action == "sell" and position > 0:
                fill_price = candle.close * (1 - self.slippage_bps / 10000)
                proceeds = position * fill_price
                fee = proceeds * self.fee_rate
                pnl = (fill_price - entry_price) * position - fee
                capital += (proceeds - fee)
                total_fees += fee
                trades.append(BacktestTrade(
                    ts=candle.ts, side="sell", price=fill_price,
                    quantity=position, fee=fee, pnl=pnl, reason=strategy.name,
                ))
                position = 0.0

            # Track equity
            equity = capital + position * candle.close
            equity_curve.append(equity)
            if len(equity_curve) > 1:
                daily_returns.append(
                    (equity_curve[-1] - equity_curve[-2]) / equity_curve[-2]
                )
            if position > 0:
                time_in_position += 1

        # Close any open position at end
        if position > 0 and candles:
            final_price = candles[-1].close
            proceeds = position * final_price
            fee = proceeds * self.fee_rate
            pnl = (final_price - entry_price) * position - fee
            capital += (proceeds - fee)
            total_fees += fee
            trades.append(BacktestTrade(
                ts=candles[-1].ts, side="sell", price=final_price,
                quantity=position, fee=fee, pnl=pnl, reason="close_eod",
            ))

        # Calculate metrics
        final_equity = capital
        total_pnl = final_equity - self.initial_capital
        total_return = total_pnl / self.initial_capital

        # Sharpe ratio (annualized)
        if daily_returns:
            avg_ret = sum(daily_returns) / len(daily_returns)
            std_ret = (sum((r - avg_ret) ** 2 for r in daily_returns) / len(daily_returns)) ** 0.5
            sharpe = (avg_ret / std_ret * math.sqrt(365)) if std_ret > 0 else 0
        else:
            sharpe = 0

        # Sortino ratio
        if daily_returns:
            neg_returns = [r for r in daily_returns if r < 0]
            downside_std = (sum(r ** 2 for r in neg_returns) / max(len(neg_returns), 1)) ** 0.5
            avg_ret = sum(daily_returns) / len(daily_returns)
            sortino = (avg_ret / downside_std * math.sqrt(365)) if downside_std > 0 else 0
        else:
            sortino = 0

        # Max drawdown
        peak = self.initial_capital
        max_dd = 0.0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = (peak - eq) / peak
            if dd > max_dd:
                max_dd = dd

        # Win rate & profit factor
        sell_trades = [t for t in trades if t.side == "sell"]
        winners = [t for t in sell_trades if t.pnl > 0]
        losers = [t for t in sell_trades if t.pnl <= 0]
        win_rate = len(winners) / len(sell_trades) if sell_trades else 0
        gross_profit = sum(t.pnl for t in winners)
        gross_loss = abs(sum(t.pnl for t in losers))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        return BacktestResult(
            strategy_name=strategy.name,
            asset="",
            period_start=candles[0].ts if candles else 0,
            period_end=candles[-1].ts if candles else 0,
            trades=trades,
            total_pnl=total_pnl,
            total_return_pct=total_return * 100,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            max_drawdown_pct=max_dd * 100,
            calmar_ratio=abs(total_return / max_dd) if max_dd > 0 else 0,
            win_rate=win_rate,
            profit_factor=profit_factor,
            avg_trade_pnl=total_pnl / len(sell_trades) if sell_trades else 0,
            total_fees=total_fees,
            num_trades=len(sell_trades),
            holding_time_avg=time_in_position / max(len(sell_trades), 1),
            exposure_pct=(time_in_position / total_candles * 100) if total_candles else 0,
        )

    def walk_forward(self, strategy_factory: Callable[[], BacktestStrategy],
                     candles: list[OHLCV], in_sample_pct: float = 0.7,
                     n_folds: int = 3) -> BacktestResult:
        """Walk-forward analysis with multiple folds."""
        fold_size = len(candles) // n_folds
        in_sample_results = []
        out_sample_results = []

        for i in range(n_folds):
            start = i * fold_size
            end = min(start + fold_size, len(candles))
            fold = candles[start:end]

            split = int(len(fold) * in_sample_pct)
            in_sample = fold[:split]
            out_sample = fold[split:]

            # In-sample
            is_result = self.run(strategy_factory(), in_sample)
            in_sample_results.append(is_result)

            # Out-of-sample
            os_result = self.run(strategy_factory(), out_sample)
            out_sample_results.append(os_result)

        # Full backtest for complete metrics
        full_result = self.run(strategy_factory(), candles)

        # Average walk-forward metrics
        if in_sample_results:
            full_result.in_sample_sharpe = sum(
                r.sharpe_ratio for r in in_sample_results) / len(in_sample_results)
        if out_sample_results:
            full_result.out_sample_sharpe = sum(
                r.sharpe_ratio for r in out_sample_results) / len(out_sample_results)

        return full_result

    def monte_carlo(self, trades: list[BacktestTrade],
                    n_simulations: int = 1000) -> tuple[float, float, float]:
        """Monte Carlo simulation — shuffle trade order for confidence intervals."""
        if not trades:
            return 0.0, 0.0, 0.0

        sell_trades = [t for t in trades if t.side == "sell"]
        pnl_values = [t.pnl for t in sell_trades]
        if not pnl_values:
            return 0.0, 0.0, 0.0

        final_pnls = []
        for _ in range(n_simulations):
            shuffled = pnl_values[:]
            random.shuffle(shuffled)
            cumulative = self.initial_capital
            for pnl in shuffled:
                cumulative += pnl
            final_pnls.append(cumulative - self.initial_capital)

        final_pnls.sort()
        median = final_pnls[len(final_pnls) // 2]
        p5 = final_pnls[int(len(final_pnls) * 0.05)]
        p95 = final_pnls[int(len(final_pnls) * 0.95)]
        return median, p5, p95


# ---------------------------------------------------------------------------
# Backtester Agent — integrates with swarm Bus
# ---------------------------------------------------------------------------
class BacktesterAgent:
    """Autonomous backtesting agent that validates strategies.

    Periodically fetches fresh data, runs all strategies through
    walk-forward + Monte Carlo, and publishes validated strategies
    to the Bus for the Strategist to consume.
    """

    def __init__(self, bus: Bus, assets: list[str] | None = None,
                 interval: float = 3600.0,
                 min_sharpe: float = 0.5,
                 min_win_rate: float = 0.45,
                 max_drawdown: float = 25.0):
        self.bus = bus
        self.assets = assets or ["BTC", "ETH", "SOL"]
        self.interval = interval
        self.min_sharpe = min_sharpe
        self.min_win_rate = min_win_rate
        self.max_drawdown = max_drawdown  # percent
        self.fetcher = HistoricalDataFetcher()
        self.engine = BacktestEngine()
        self._running = False
        self._results: dict[str, list[BacktestResult]] = {}

    async def run(self):
        self._running = True
        log.info("Backtester agent started: assets=%s strategies=%d interval=%.0fs",
                 self.assets, len(STRATEGIES), self.interval)

        while self._running:
            await self._run_all_backtests()
            await asyncio.sleep(self.interval)

    async def stop(self):
        self._running = False
        await self.fetcher.close()

    async def _run_all_backtests(self):
        """Run all strategies on all assets."""
        for asset in self.assets:
            candles = await self.fetcher.fetch_ohlcv(asset, days=365)
            if len(candles) < 30:
                log.warning("Insufficient data for %s (%d candles)", asset, len(candles))
                continue

            asset_results = []
            for strategy_name, factory in STRATEGIES.items():
                try:
                    # Walk-forward backtest
                    result = self.engine.walk_forward(factory, candles)
                    result.asset = asset

                    # Monte Carlo
                    mc_med, mc_5, mc_95 = self.engine.monte_carlo(result.trades)
                    result.mc_median_pnl = mc_med
                    result.mc_5th_percentile = mc_5
                    result.mc_95th_percentile = mc_95

                    asset_results.append(result)

                    # Publish result
                    await self.bus.publish("backtest.result", {
                        "strategy": result.strategy_name,
                        "asset": asset,
                        "total_pnl": round(result.total_pnl, 2),
                        "return_pct": round(result.total_return_pct, 2),
                        "sharpe": round(result.sharpe_ratio, 2),
                        "sortino": round(result.sortino_ratio, 2),
                        "max_dd_pct": round(result.max_drawdown_pct, 2),
                        "win_rate": round(result.win_rate, 3),
                        "profit_factor": round(result.profit_factor, 2),
                        "num_trades": result.num_trades,
                        "in_sample_sharpe": round(result.in_sample_sharpe, 2),
                        "out_sample_sharpe": round(result.out_sample_sharpe, 2),
                        "mc_median": round(mc_med, 2),
                        "mc_5th": round(mc_5, 2),
                        "mc_95th": round(mc_95, 2),
                        "ts": time.time(),
                    })

                    # Check if strategy passes validation
                    if self._is_validated(result):
                        await self.bus.publish("strategy.validated", {
                            "strategy": result.strategy_name,
                            "asset": asset,
                            "sharpe": round(result.sharpe_ratio, 2),
                            "win_rate": round(result.win_rate, 3),
                            "max_dd": round(result.max_drawdown_pct, 2),
                            "out_sample_sharpe": round(result.out_sample_sharpe, 2),
                            "mc_5th_pct": round(mc_5, 2),
                        })
                        log.info("VALIDATED: %s on %s — Sharpe=%.2f WR=%.1f%% DD=%.1f%%",
                                 strategy_name, asset, result.sharpe_ratio,
                                 result.win_rate * 100, result.max_drawdown_pct)

                except Exception as e:
                    log.warning("Backtest failed: %s/%s — %s", strategy_name, asset, e)

            self._results[asset] = asset_results

            # Publish intelligence summary
            if asset_results:
                best = max(asset_results, key=lambda r: r.sharpe_ratio)
                await self.bus.publish("intelligence.backtest", {
                    "asset": asset,
                    "best_strategy": best.strategy_name,
                    "best_sharpe": round(best.sharpe_ratio, 2),
                    "strategies_tested": len(asset_results),
                    "strategies_validated": sum(1 for r in asset_results if self._is_validated(r)),
                    "ts": time.time(),
                })

    def _is_validated(self, result: BacktestResult) -> bool:
        """Check if a backtest result passes validation thresholds."""
        return (
            result.sharpe_ratio >= self.min_sharpe
            and result.win_rate >= self.min_win_rate
            and result.max_drawdown_pct <= self.max_drawdown
            and result.num_trades >= 10
            and result.out_sample_sharpe > 0  # Must be profitable OOS
            and result.mc_5th_percentile > -self.engine.initial_capital * 0.1  # 5th %ile > -10%
        )

    def get_best_strategy(self, asset: str) -> BacktestResult | None:
        """Get the best validated strategy for an asset."""
        results = self._results.get(asset, [])
        validated = [r for r in results if self._is_validated(r)]
        if not validated:
            return None
        return max(validated, key=lambda r: r.sharpe_ratio)

    def get_leaderboard(self) -> list[dict]:
        """Get strategy leaderboard across all assets."""
        board = []
        for asset, results in self._results.items():
            for r in results:
                board.append({
                    "strategy": r.strategy_name,
                    "asset": asset,
                    "sharpe": round(r.sharpe_ratio, 2),
                    "return_pct": round(r.total_return_pct, 2),
                    "win_rate": round(r.win_rate * 100, 1),
                    "max_dd": round(r.max_drawdown_pct, 1),
                    "validated": self._is_validated(r),
                })
        board.sort(key=lambda x: x["sharpe"], reverse=True)
        return board
