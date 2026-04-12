"""Walk-forward backtesting, transaction cost analysis, and Monte Carlo simulation.

Extends the existing backtest engine with:
- Walk-forward cross-validation to detect overfitting
- Transaction cost analysis (TCA) for execution quality
- Monte Carlo bootstrapping for confidence intervals

Uses only stdlib (math, statistics, random, collections, time).
"""
from __future__ import annotations

import asyncio
import logging
import math
import random
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

from .core import (
    Bus,
    ExecutionReport,
    MarketSnapshot,
    PortfolioTracker,
)

log = logging.getLogger("swarm.walkforward")


# =====================================================================
# Walk-Forward Engine
# =====================================================================

@dataclass
class FoldResult:
    """Metrics for a single walk-forward fold."""

    fold_idx: int
    train_size: int
    test_size: int
    in_sample_sharpe: float
    out_sample_sharpe: float
    pnl: float
    win_rate: float
    max_dd: float
    trades: int


@dataclass
class WalkForwardResult:
    """Aggregate walk-forward results across all folds."""

    folds: list[FoldResult] = field(default_factory=list)
    aggregate_sharpe: float = 0.0
    aggregate_sortino: float = 0.0
    aggregate_max_dd: float = 0.0
    avg_win_rate: float = 0.0
    total_pnl: float = 0.0
    overfit_score: float = 0.0  # IS sharpe / OOS sharpe; >2.0 = likely overfit

    def summary(self) -> str:
        lines = [
            "=" * 64,
            "  WALK-FORWARD RESULTS",
            "=" * 64,
            f"  Folds             : {len(self.folds)}",
            f"  Total PnL         : ${self.total_pnl:+,.2f}",
            f"  Aggregate Sharpe  : {self.aggregate_sharpe:.3f}",
            f"  Aggregate Sortino : {self.aggregate_sortino:.3f}",
            f"  Aggregate Max DD  : {self.aggregate_max_dd:.2%}",
            f"  Avg Win Rate      : {self.avg_win_rate:.1%}",
            f"  Overfit Score     : {self.overfit_score:.2f}"
            + ("  (LIKELY OVERFIT)" if self.overfit_score > 2.0 else ""),
            "-" * 64,
            f"  {'Fold':>4}  {'Train':>6}  {'Test':>5}  {'IS Sharpe':>10}  "
            f"{'OOS Sharpe':>10}  {'PnL':>10}  {'WR':>6}  {'MaxDD':>7}  {'Trades':>6}",
            "-" * 64,
        ]
        for f in self.folds:
            lines.append(
                f"  {f.fold_idx:>4}  {f.train_size:>6}  {f.test_size:>5}  "
                f"{f.in_sample_sharpe:>10.3f}  {f.out_sample_sharpe:>10.3f}  "
                f"${f.pnl:>+9,.2f}  {f.win_rate:>5.1%}  "
                f"{f.max_dd:>6.2%}  {f.trades:>6}"
            )
        lines.append("=" * 64)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "folds": [
                {
                    "fold_idx": f.fold_idx,
                    "train_size": f.train_size,
                    "test_size": f.test_size,
                    "in_sample_sharpe": round(f.in_sample_sharpe, 4),
                    "out_sample_sharpe": round(f.out_sample_sharpe, 4),
                    "pnl": round(f.pnl, 4),
                    "win_rate": round(f.win_rate, 4),
                    "max_dd": round(f.max_dd, 4),
                    "trades": f.trades,
                }
                for f in self.folds
            ],
            "aggregate_sharpe": round(self.aggregate_sharpe, 4),
            "aggregate_sortino": round(self.aggregate_sortino, 4),
            "aggregate_max_dd": round(self.aggregate_max_dd, 4),
            "avg_win_rate": round(self.avg_win_rate, 4),
            "total_pnl": round(self.total_pnl, 4),
            "overfit_score": round(self.overfit_score, 4),
        }


def _compute_sharpe(returns: list[float]) -> float:
    """Annualized Sharpe from a list of per-trade returns."""
    if len(returns) < 2:
        return 0.0
    mean_r = statistics.mean(returns)
    std_r = statistics.pstdev(returns)
    if std_r < 1e-12:
        return 0.0
    return (mean_r / std_r) * math.sqrt(min(252, len(returns)))


def _compute_sortino(returns: list[float]) -> float:
    """Annualized Sortino ratio (downside deviation only)."""
    if len(returns) < 2:
        return 0.0
    mean_r = statistics.mean(returns)
    downside = [r for r in returns if r < 0]
    if not downside:
        return float("inf") if mean_r > 0 else 0.0
    dd_var = statistics.mean([d * d for d in downside])
    dd_std = math.sqrt(dd_var)
    if dd_std < 1e-12:
        return 0.0
    return (mean_r / dd_std) * math.sqrt(min(252, len(returns)))


def _compute_max_drawdown(equity_curve: list[float]) -> float:
    """Max drawdown from an equity curve."""
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        peak = max(peak, val)
        dd = (peak - val) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return max_dd


def _candle_returns(candles: list[list]) -> list[float]:
    """Extract per-candle returns from OHLC data (close-to-close)."""
    returns = []
    for i in range(1, len(candles)):
        prev_close = float(candles[i - 1][4])
        curr_close = float(candles[i][4])
        if prev_close > 0:
            returns.append((curr_close - prev_close) / prev_close)
    return returns


class WalkForwardEngine:
    """Walk-forward cross-validation engine.

    Splits candle data into expanding (or sliding) train windows and
    strictly out-of-sample test windows. Runs the full agent pipeline
    on each test window and collects per-fold metrics.

    Parameters
    ----------
    run_pipeline : async callable
        ``async def run_pipeline(candles) -> (pnl_list, equity_curve)``
        Runs the full signal -> strategy -> risk -> execution pipeline
        on a list of candles and returns per-trade PnL values and an
        equity curve.  The caller wires this up with the same agent
        config used in live trading.
    """

    def __init__(self, run_pipeline):
        self.run_pipeline = run_pipeline

    async def run(
        self,
        candles: list[list],
        train_pct: float = 0.6,
        n_folds: int = 5,
        retrain: bool = True,
    ) -> WalkForwardResult:
        """Execute walk-forward analysis.

        Parameters
        ----------
        candles : list[list]
            OHLC candles ``[ts, o, h, l, c, vwap, vol, count]``.
        train_pct : float
            Fraction of data for the initial training window.
        n_folds : int
            Number of out-of-sample test folds.
        retrain : bool
            If True, use expanding window; otherwise fixed-size sliding.
        """
        n = len(candles)
        if n < 20:
            raise ValueError(f"Need >= 20 candles, got {n}")

        initial_train = int(n * train_pct)
        remaining = n - initial_train
        fold_size = max(1, remaining // n_folds)

        result = WalkForwardResult()
        all_oos_returns: list[float] = []
        all_is_sharpes: list[float] = []
        all_oos_sharpes: list[float] = []
        oos_equity: list[float] = []
        cumulative_pnl = 0.0

        for fold_idx in range(n_folds):
            test_start = initial_train + fold_idx * fold_size
            test_end = min(test_start + fold_size, n)
            if test_start >= n:
                break

            # Train window
            if retrain:
                train_start = 0  # expanding
            else:
                train_start = max(0, test_start - initial_train)  # sliding

            train_candles = candles[train_start:test_start]
            test_candles = candles[test_start:test_end]

            if len(train_candles) < 5 or len(test_candles) < 1:
                continue

            # In-sample metrics (from candle returns as proxy)
            is_returns = _candle_returns(train_candles)
            is_sharpe = _compute_sharpe(is_returns) if is_returns else 0.0

            # Out-of-sample: run the full pipeline
            try:
                oos_pnls, oos_eq = await self.run_pipeline(test_candles)
            except Exception:
                log.exception("Pipeline failed on fold %d", fold_idx)
                oos_pnls, oos_eq = [], []

            oos_sharpe = _compute_sharpe(oos_pnls) if oos_pnls else 0.0
            fold_pnl = sum(oos_pnls)
            wins = sum(1 for p in oos_pnls if p > 0)
            wr = wins / len(oos_pnls) if oos_pnls else 0.0
            fold_dd = _compute_max_drawdown(oos_eq) if oos_eq else 0.0

            fold_result = FoldResult(
                fold_idx=fold_idx,
                train_size=len(train_candles),
                test_size=len(test_candles),
                in_sample_sharpe=is_sharpe,
                out_sample_sharpe=oos_sharpe,
                pnl=fold_pnl,
                win_rate=wr,
                max_dd=fold_dd,
                trades=len(oos_pnls),
            )
            result.folds.append(fold_result)
            all_oos_returns.extend(oos_pnls)
            all_is_sharpes.append(is_sharpe)
            all_oos_sharpes.append(oos_sharpe)
            cumulative_pnl += fold_pnl
            for p in oos_pnls:
                cumulative_pnl_at = sum(all_oos_returns[: len(oos_equity) + 1])
                oos_equity.append(cumulative_pnl_at)

            log.info(
                "Fold %d/%d: train=%d test=%d IS_sharpe=%.3f OOS_sharpe=%.3f pnl=%.2f",
                fold_idx + 1,
                n_folds,
                len(train_candles),
                len(test_candles),
                is_sharpe,
                oos_sharpe,
                fold_pnl,
            )

        # Aggregate metrics
        result.total_pnl = sum(f.pnl for f in result.folds)
        result.aggregate_sharpe = _compute_sharpe(all_oos_returns)
        result.aggregate_sortino = _compute_sortino(all_oos_returns)
        result.aggregate_max_dd = _compute_max_drawdown(oos_equity)
        result.avg_win_rate = (
            statistics.mean([f.win_rate for f in result.folds]) if result.folds else 0.0
        )

        # Overfit score: mean IS Sharpe / mean OOS Sharpe
        avg_is = statistics.mean(all_is_sharpes) if all_is_sharpes else 0.0
        avg_oos = statistics.mean(all_oos_sharpes) if all_oos_sharpes else 0.0
        if abs(avg_oos) > 1e-9:
            result.overfit_score = abs(avg_is / avg_oos)
        else:
            result.overfit_score = float("inf") if avg_is > 0 else 0.0

        if result.overfit_score > 2.0:
            log.warning(
                "OVERFIT WARNING: IS/OOS Sharpe ratio = %.2f (threshold: 2.0)",
                result.overfit_score,
            )

        return result


# =====================================================================
# Transaction Cost Analysis (TCA)
# =====================================================================

@dataclass
class _TradeRecord:
    """Internal record for TCA tracking."""

    intent_id: str
    signal_ts: float
    arrival_price: float
    fill_price: float
    fill_ts: float
    side: str
    quantity: float
    asset: str
    fee_usd: float


class TransactionCostAnalyzer:
    """Measures execution quality: slippage, market impact, timing cost.

    Subscribes to ``exec.report`` and ``market.snapshot``.
    Publishes periodic ``tca.report`` summaries.
    """

    def __init__(
        self,
        bus: Bus,
        report_interval_s: float = 300.0,
    ):
        self.bus = bus
        self.report_interval_s = report_interval_s
        self._trades: list[_TradeRecord] = []
        self._last_prices: dict[str, float] = {}
        self._vwap_accum: dict[str, list[tuple[float, float]]] = {}  # asset: [(price, qty)]
        self._signal_prices: dict[str, float] = {}  # intent_id -> arrival price
        self._running = False

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("exec.report", self._on_report)
        bus.subscribe("intent.new", self._on_intent)

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        self._last_prices.update(snap.prices)

    async def _on_intent(self, intent) -> None:
        """Capture the arrival price at signal time."""
        from .core import TradeIntent, QUOTE_ASSETS

        asset = intent.asset_out if intent.asset_in.upper() in QUOTE_ASSETS else intent.asset_in
        price = self._last_prices.get(asset, 0.0)
        if price > 0:
            self._signal_prices[intent.id] = price

    async def _on_report(self, rep: ExecutionReport) -> None:
        if rep.status != "filled" or rep.fill_price is None:
            return

        arrival = self._signal_prices.pop(rep.intent_id, rep.fill_price)
        record = _TradeRecord(
            intent_id=rep.intent_id,
            signal_ts=0.0,
            arrival_price=arrival,
            fill_price=rep.fill_price,
            fill_ts=time.time(),
            side=rep.side,
            quantity=rep.quantity,
            asset=rep.asset,
            fee_usd=rep.fee_usd,
        )
        self._trades.append(record)

        # Accumulate for VWAP
        vwap_list = self._vwap_accum.setdefault(rep.asset, [])
        vwap_list.append((rep.fill_price, rep.quantity))

    def _vwap(self, asset: str) -> float:
        """Compute VWAP from accumulated fills."""
        entries = self._vwap_accum.get(asset, [])
        if not entries:
            return 0.0
        total_value = sum(p * q for p, q in entries)
        total_qty = sum(q for _, q in entries)
        return total_value / total_qty if total_qty > 0 else 0.0

    def implementation_shortfall(self) -> float:
        """Average implementation shortfall in basis points.

        ``(fill_price - arrival_price) / arrival_price * 10000``
        Sign convention: positive = cost (filled worse than arrival).
        """
        if not self._trades:
            return 0.0
        bps_list = []
        for t in self._trades:
            if t.arrival_price <= 0:
                continue
            diff = t.fill_price - t.arrival_price
            if t.side == "sell":
                diff = -diff  # selling higher is good
            bps_list.append((diff / t.arrival_price) * 10_000)
        return statistics.mean(bps_list) if bps_list else 0.0

    def vs_vwap(self) -> float:
        """Average deviation from VWAP in basis points."""
        if not self._trades:
            return 0.0
        bps_list = []
        for t in self._trades:
            vwap = self._vwap(t.asset)
            if vwap <= 0:
                continue
            diff = t.fill_price - vwap
            if t.side == "sell":
                diff = -diff
            bps_list.append((diff / vwap) * 10_000)
        return statistics.mean(bps_list) if bps_list else 0.0

    def market_impact(self) -> float:
        """Estimated market impact in basis points.

        Uses a simple model: impact = 0.5 * shortfall (temporary)
        + 0.5 * post-trade price move (permanent proxy).
        With limited data we approximate permanent impact as half
        the shortfall.
        """
        shortfall = self.implementation_shortfall()
        return abs(shortfall) * 0.75  # blended estimate

    def timing_cost(self) -> float:
        """Average timing cost in basis points.

        Measures cost of delay: difference between arrival price
        and the price at fill time (approximated by arrival vs last
        known price before fill).
        """
        if not self._trades:
            return 0.0
        bps_list = []
        for t in self._trades:
            if t.arrival_price <= 0:
                continue
            current = self._last_prices.get(t.asset, t.fill_price)
            diff = current - t.arrival_price
            if t.side == "sell":
                diff = -diff
            bps_list.append((diff / t.arrival_price) * 10_000)
        return statistics.mean(bps_list) if bps_list else 0.0

    def tca_report(self) -> dict:
        """Aggregate TCA statistics."""
        total_fees = sum(t.fee_usd for t in self._trades)
        total_notional = sum(t.fill_price * t.quantity for t in self._trades)
        return {
            "trade_count": len(self._trades),
            "avg_shortfall_bps": round(self.implementation_shortfall(), 2),
            "avg_vwap_deviation_bps": round(self.vs_vwap(), 2),
            "avg_impact_bps": round(self.market_impact(), 2),
            "avg_timing_cost_bps": round(self.timing_cost(), 2),
            "total_execution_cost_usd": round(total_fees, 4),
            "total_notional_usd": round(total_notional, 2),
            "cost_per_10k_notional": (
                round(total_fees / (total_notional / 10_000), 2)
                if total_notional > 0
                else 0.0
            ),
        }

    async def run(self) -> None:
        """Periodically publish TCA report."""
        self._running = True
        while self._running:
            await asyncio.sleep(self.report_interval_s)
            report = self.tca_report()
            log.info("TCA report: %s", report)
            await self.bus.publish("tca.report", report)

    def stop(self) -> None:
        self._running = False


# =====================================================================
# Monte Carlo Backtester
# =====================================================================

class MonteCarloBacktester:
    """Bootstrap Monte Carlo simulation for strategy confidence intervals.

    Takes historical per-trade returns and generates synthetic return
    paths by sampling with replacement (block bootstrap).
    """

    def __init__(self, returns: list[float], seed: int | None = None):
        if not returns:
            raise ValueError("returns must be non-empty")
        self.returns = list(returns)
        self._rng = random.Random(seed)

    def simulate(
        self,
        n_paths: int = 1000,
        horizon: int = 252,
    ) -> dict:
        """Run Monte Carlo bootstrap simulation.

        Parameters
        ----------
        n_paths : int
            Number of synthetic paths to generate.
        horizon : int
            Length of each path (e.g. 252 trading days).

        Returns
        -------
        dict with keys: median_return, p5_return, p95_return,
        probability_of_loss, expected_max_drawdown,
        sharpe_distribution.
        """
        final_returns: list[float] = []
        max_drawdowns: list[float] = []
        sharpe_values: list[float] = []

        for _ in range(n_paths):
            # Bootstrap: sample returns with replacement
            path_returns = [
                self._rng.choice(self.returns) for _ in range(horizon)
            ]

            # Build equity curve from cumulative returns
            equity = [1.0]
            for r in path_returns:
                equity.append(equity[-1] * (1.0 + r))

            final_return = equity[-1] / equity[0] - 1.0
            final_returns.append(final_return)

            # Max drawdown for this path
            peak = equity[0]
            max_dd = 0.0
            for val in equity:
                peak = max(peak, val)
                dd = (peak - val) / peak if peak > 0 else 0.0
                max_dd = max(max_dd, dd)
            max_drawdowns.append(max_dd)

            # Sharpe for this path
            if len(path_returns) >= 2:
                mean_r = statistics.mean(path_returns)
                std_r = statistics.pstdev(path_returns)
                if std_r > 1e-12:
                    sharpe_values.append(
                        (mean_r / std_r) * math.sqrt(min(252, horizon))
                    )
                else:
                    sharpe_values.append(0.0)
            else:
                sharpe_values.append(0.0)

        final_returns.sort()
        max_drawdowns.sort()

        n = len(final_returns)
        p5_idx = max(0, int(n * 0.05))
        p95_idx = min(n - 1, int(n * 0.95))

        prob_loss = sum(1 for r in final_returns if r < 0) / n

        return {
            "median_return": round(statistics.median(final_returns), 6),
            "p5_return": round(final_returns[p5_idx], 6),
            "p95_return": round(final_returns[p95_idx], 6),
            "probability_of_loss": round(prob_loss, 4),
            "expected_max_drawdown": round(statistics.mean(max_drawdowns), 6),
            "sharpe_distribution": [round(s, 4) for s in sorted(sharpe_values)],
        }
