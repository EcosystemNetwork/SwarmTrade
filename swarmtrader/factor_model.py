"""Factor model, real-time PnL attribution, and factor-based risk analysis.

Provides systematic factor decomposition of crypto returns, attribution of
PnL to factors vs alpha, and factor-concentration risk checks that plug
into the RiskAgent pipeline.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .core import (
    Bus,
    ExecutionReport,
    MarketSnapshot,
    PortfolioTracker,
    TradeIntent,
)

log = logging.getLogger("swarm.factor")

# ---------------------------------------------------------------------------
# Helpers (stdlib only — no numpy)
# ---------------------------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _stdev(xs: list[float], mu: float | None = None) -> float:
    if len(xs) < 2:
        return 0.0
    m = mu if mu is not None else _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _cov(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    mx, my = _mean(xs[:n]), _mean(ys[:n])
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / (n - 1)


def _zscore(value: float, mu: float, sigma: float) -> float:
    if sigma < 1e-12:
        return 0.0
    return (value - mu) / sigma


# ---------------------------------------------------------------------------
# Factor dataclass
# ---------------------------------------------------------------------------

@dataclass
class Factor:
    """A single systematic factor with per-asset exposures."""
    name: str
    description: str
    values: dict[str, float] = field(default_factory=dict)   # asset -> exposure


# ---------------------------------------------------------------------------
# FactorModel — computes factor exposures and decomposes returns
# ---------------------------------------------------------------------------

FACTOR_NAMES = ("market", "momentum", "value", "volatility", "carry", "liquidity")
MOMENTUM_WINDOW = 20
VALUE_WINDOW = 50
VOL_WINDOW = 20


class FactorModel:
    """Systematic multi-factor model for crypto assets.

    Subscribes to ``market.snapshot`` to build rolling return series and
    computes six canonical factors: market, momentum, value, volatility,
    carry, and liquidity.
    """

    def __init__(self, bus: Bus, max_history: int = 200):
        self.bus = bus
        self.max_history = max_history

        # Rolling price / return histories keyed by asset
        self._prices: dict[str, deque[float]] = {}
        self._returns: dict[str, deque[float]] = {}

        # Market return series (average of BTC + ETH returns)
        self._market_returns: deque[float] = deque(maxlen=max_history)

        # Spread data for liquidity factor (set externally or via snapshot)
        self._spreads: dict[str, float] = {}

        # Funding rate proxy (set externally)
        self._funding: dict[str, float] = {}

        # Current factor objects
        self.factors: dict[str, Factor] = {
            "market": Factor("market", "Beta to crypto market (BTC+ETH average)"),
            "momentum": Factor("momentum", f"Trailing {MOMENTUM_WINDOW}-period return"),
            "value": Factor("value", f"Mean-reversion z-score from {VALUE_WINDOW}-period mean"),
            "volatility": Factor("volatility", "Inverse realised volatility (low-vol positive)"),
            "carry": Factor("carry", "Funding rate proxy"),
            "liquidity": Factor("liquidity", "Inverse spread proxy"),
        }

        self._last_snapshot_ts: float = 0.0
        bus.subscribe("market.snapshot", self._on_snapshot)

    # ── snapshot handler ──────────────────────────────────────────
    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        prices = snap.prices
        self._last_snapshot_ts = snap.ts

        # --- compute per-asset returns ---
        for asset, price in prices.items():
            if asset not in self._prices:
                self._prices[asset] = deque(maxlen=self.max_history)
                self._returns[asset] = deque(maxlen=self.max_history)

            hist = self._prices[asset]
            if hist:
                prev = hist[-1]
                ret = (price - prev) / prev if prev > 1e-12 else 0.0
                self._returns[asset].append(ret)
            hist.append(price)

        # --- market return (BTC + ETH average) ---
        btc_ret = self._returns.get("BTC", deque())
        eth_ret = self._returns.get("ETH", deque())
        components: list[float] = []
        if btc_ret:
            components.append(btc_ret[-1])
        if eth_ret:
            components.append(eth_ret[-1])
        mkt = _mean(components) if components else 0.0
        self._market_returns.append(mkt)

        # --- recompute factor exposures ---
        self._recompute(prices)

    # ── internal factor computation ───────────────────────────────
    def _recompute(self, prices: dict[str, float]) -> None:
        for asset in prices:
            rets = list(self._returns.get(asset, []))
            price_hist = list(self._prices.get(asset, []))

            # market beta
            mkt_list = list(self._market_returns)
            n = min(len(rets), len(mkt_list))
            if n >= 5:
                asset_slice = rets[-n:]
                mkt_slice = mkt_list[-n:]
                var_mkt = _cov(mkt_slice, mkt_slice)
                cov_am = _cov(asset_slice, mkt_slice)
                beta = cov_am / var_mkt if var_mkt > 1e-14 else 1.0
            else:
                beta = 1.0
            self.factors["market"].values[asset] = beta

            # momentum — trailing 20-period return
            if len(price_hist) > MOMENTUM_WINDOW:
                old_p = price_hist[-(MOMENTUM_WINDOW + 1)]
                cur_p = price_hist[-1]
                mom = (cur_p - old_p) / old_p if old_p > 1e-12 else 0.0
            else:
                mom = 0.0
            self.factors["momentum"].values[asset] = mom

            # value — z-score from 50-period mean (negative z = undervalued)
            if len(price_hist) >= VALUE_WINDOW:
                window = price_hist[-VALUE_WINDOW:]
                mu = _mean(window)
                sigma = _stdev(window, mu)
                z = _zscore(price_hist[-1], mu, sigma)
                # Invert: undervalued (low z) = positive value exposure
                self.factors["value"].values[asset] = -z
            else:
                self.factors["value"].values[asset] = 0.0

            # volatility — inverse realised vol (low vol = positive)
            if len(rets) >= VOL_WINDOW:
                vol = _stdev(rets[-VOL_WINDOW:])
                self.factors["volatility"].values[asset] = (1.0 / vol) if vol > 1e-12 else 0.0
            else:
                self.factors["volatility"].values[asset] = 0.0

            # carry — funding rate proxy
            self.factors["carry"].values[asset] = self._funding.get(asset, 0.0)

            # liquidity — inverse spread
            spread = self._spreads.get(asset, 0.0)
            self.factors["liquidity"].values[asset] = (1.0 / spread) if spread > 1e-8 else 0.0

    # ── public API ────────────────────────────────────────────────
    def set_spread(self, asset: str, spread: float) -> None:
        """Update spread data for an asset (bid-ask spread as fraction)."""
        self._spreads[asset] = spread

    def set_funding(self, asset: str, rate: float) -> None:
        """Update funding rate proxy for an asset."""
        self._funding[asset] = rate

    def factor_exposures(self, asset: str) -> dict[str, float]:
        """Current factor loadings for *asset*."""
        return {name: f.values.get(asset, 0.0) for name, f in self.factors.items()}

    def factor_returns(self) -> dict[str, float]:
        """Latest period return attributable to each factor.

        Uses cross-sectional average of (factor_exposure * asset_return) as a
        simple proxy for the factor's return contribution.
        """
        result: dict[str, float] = {}
        for fname in FACTOR_NAMES:
            factor = self.factors[fname]
            contrib_sum = 0.0
            count = 0
            for asset, exposure in factor.values.items():
                rets = self._returns.get(asset)
                if rets:
                    contrib_sum += exposure * rets[-1]
                    count += 1
            result[fname] = contrib_sum / count if count else 0.0
        return result

    def decompose_return(self, asset: str, period_return: float) -> dict[str, float]:
        """Decompose *period_return* of *asset* into factor contributions + alpha."""
        exposures = self.factor_exposures(asset)
        f_rets = self.factor_returns()
        decomp: dict[str, float] = {}
        explained = 0.0
        for fname in FACTOR_NAMES:
            contrib = exposures.get(fname, 0.0) * f_rets.get(fname, 0.0)
            decomp[fname] = contrib
            explained += contrib
        decomp["alpha"] = period_return - explained
        return decomp

    def orthogonalize_signal(self, signal_strength: float, asset: str) -> float:
        """Remove factor-explained component, return pure alpha signal.

        Subtracts the expected return predicted by factor exposures so that
        only the idiosyncratic (alpha) portion of the signal remains.
        """
        exposures = self.factor_exposures(asset)
        f_rets = self.factor_returns()
        factor_predicted = sum(
            exposures.get(f, 0.0) * f_rets.get(f, 0.0) for f in FACTOR_NAMES
        )
        return signal_strength - factor_predicted


# ---------------------------------------------------------------------------
# PnLAttributor — real-time PnL decomposition by signal, factor, asset, time
# ---------------------------------------------------------------------------

class PnLAttributor:
    """Tracks and attributes PnL across multiple dimensions.

    Subscribes to ``exec.report`` for realized trades and ``market.snapshot``
    for mark-to-market.  Publishes ``pnl.attribution`` every 30 seconds.
    """

    def __init__(
        self,
        bus: Bus,
        factor_model: FactorModel,
        portfolio: PortfolioTracker,
        publish_interval: float = 30.0,
    ):
        self.bus = bus
        self.fm = factor_model
        self.portfolio = portfolio
        self.publish_interval = publish_interval

        # realized tracking
        self._realized_pnl: float = 0.0
        self._by_signal: dict[str, float] = {}     # agent_id -> pnl
        self._by_factor: dict[str, float] = {f: 0.0 for f in FACTOR_NAMES}
        self._alpha_pnl: float = 0.0
        self._by_asset: dict[str, float] = {}
        self._by_hour: dict[str, float] = {}

        # for information ratio
        self._alpha_returns: list[float] = []

        # trade -> supporting signals mapping (intent_id -> list[agent_id])
        self._intent_signals: dict[str, list[str]] = {}

        self._last_publish: float = 0.0
        self._task: asyncio.Task | None = None

        bus.subscribe("exec.report", self._on_exec)
        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("intent.new", self._on_intent)

    async def _on_intent(self, intent: TradeIntent) -> None:
        """Cache the supporting signal agents for later PnL attribution."""
        self._intent_signals[intent.id] = [
            s.agent_id for s in getattr(intent, "supporting", [])
        ]

    async def _on_exec(self, rep: ExecutionReport) -> None:
        if rep.status != "filled":
            return

        pnl = rep.pnl_estimate or 0.0
        self._realized_pnl += pnl

        # --- asset attribution ---
        asset = rep.asset or "UNKNOWN"
        self._by_asset[asset] = self._by_asset.get(asset, 0.0) + pnl

        # --- time attribution ---
        hour_key = time.strftime("%Y-%m-%d %H:00", time.gmtime(time.time()))
        self._by_hour[hour_key] = self._by_hour.get(hour_key, 0.0) + pnl

        # --- signal attribution ---
        agents = self._intent_signals.pop(rep.intent_id, [])
        if agents:
            share = pnl / len(agents)
            for agent_id in agents:
                self._by_signal[agent_id] = self._by_signal.get(agent_id, 0.0) + share

        # --- factor attribution ---
        if asset != "UNKNOWN" and abs(pnl) > 1e-9:
            # Approximate the per-unit return for the trade
            fill_price = rep.fill_price or 0.0
            qty = rep.quantity or 0.0
            if fill_price > 1e-12 and qty > 1e-12:
                unit_return = pnl / (fill_price * qty)
            else:
                unit_return = 0.0

            decomp = self.fm.decompose_return(asset, unit_return)
            trade_value = fill_price * qty if fill_price > 0 and qty > 0 else 1.0
            for fname in FACTOR_NAMES:
                contrib_pnl = decomp.get(fname, 0.0) * trade_value
                self._by_factor[fname] = self._by_factor.get(fname, 0.0) + contrib_pnl
            alpha_contrib = decomp.get("alpha", 0.0) * trade_value
            self._alpha_pnl += alpha_contrib
            self._alpha_returns.append(alpha_contrib)

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        self.portfolio.update_prices(snap.prices)

        now = time.time()
        if now - self._last_publish >= self.publish_interval:
            self._last_publish = now
            report = self.pnl_report()
            await self.bus.publish("pnl.attribution", report)
            log.info(
                "PNL total=%.4f realized=%.4f unrealized=%.4f alpha=%.4f IR=%.3f",
                report["total_pnl"],
                report["realized_pnl"],
                report["unrealized_pnl"],
                report["alpha_pnl"],
                report["information_ratio"],
            )

    def pnl_report(self) -> dict[str, Any]:
        """Comprehensive PnL report across all attribution dimensions."""
        unrealized = sum(
            pos.unrealized_pnl(self.portfolio.last_prices.get(asset, pos.avg_entry))
            for asset, pos in self.portfolio.positions.items()
            if pos.quantity > 1e-9
        )
        total = self._realized_pnl + unrealized

        # information ratio = mean(alpha) / std(alpha)
        if len(self._alpha_returns) >= 2:
            mu_a = _mean(self._alpha_returns)
            te = _stdev(self._alpha_returns, mu_a)
            ir = mu_a / te if te > 1e-12 else 0.0
        else:
            ir = 0.0

        return {
            "total_pnl": round(total, 6),
            "realized_pnl": round(self._realized_pnl, 6),
            "unrealized_pnl": round(unrealized, 6),
            "by_signal": dict(self._by_signal),
            "by_factor": dict(self._by_factor),
            "by_asset": dict(self._by_asset),
            "by_hour": dict(self._by_hour),
            "alpha_pnl": round(self._alpha_pnl, 6),
            "information_ratio": round(ir, 6),
        }


# ---------------------------------------------------------------------------
# FactorRiskModel — portfolio-level factor risk decomposition
# ---------------------------------------------------------------------------

class FactorRiskModel:
    """Factor-based risk analytics for the portfolio.

    Uses the FactorModel's rolling data to build a factor covariance matrix
    and decompose portfolio risk by factor.
    """

    def __init__(self, factor_model: FactorModel, portfolio: PortfolioTracker):
        self.fm = factor_model
        self.portfolio = portfolio

    def _factor_return_series(self) -> dict[str, list[float]]:
        """Build time-aligned factor return series from asset returns.

        For each period, the factor return is the cross-sectional mean of
        (exposure * asset_return).  This is a rough but robust estimator
        without a full regression.
        """
        # Determine the number of periods we can cover
        all_assets = list(self.fm._returns.keys())
        if not all_assets:
            return {f: [] for f in FACTOR_NAMES}

        min_len = min(len(self.fm._returns[a]) for a in all_assets) if all_assets else 0
        if min_len < 2:
            return {f: [] for f in FACTOR_NAMES}

        series: dict[str, list[float]] = {f: [] for f in FACTOR_NAMES}

        for t in range(min_len):
            for fname in FACTOR_NAMES:
                factor = self.fm.factors[fname]
                vals: list[float] = []
                for asset in all_assets:
                    exp = factor.values.get(asset, 0.0)
                    ret = list(self.fm._returns[asset])[t]
                    vals.append(exp * ret)
                series[fname].append(_mean(vals) if vals else 0.0)

        return series

    def _factor_cov_matrix(self) -> dict[str, dict[str, float]]:
        """Factor covariance matrix as nested dicts."""
        series = self._factor_return_series()
        cov: dict[str, dict[str, float]] = {}
        for f1 in FACTOR_NAMES:
            cov[f1] = {}
            for f2 in FACTOR_NAMES:
                cov[f1][f2] = _cov(series[f1], series[f2])
        return cov

    def portfolio_factor_risk(self) -> dict[str, Any]:
        """Portfolio risk decomposition by factor.

        Returns per-factor variance contribution and the total factor
        variance.
        """
        # Aggregate portfolio-level factor exposures (value-weighted)
        positions = self.portfolio.positions
        prices = self.portfolio.last_prices
        total_value = 0.0
        port_exposures: dict[str, float] = {f: 0.0 for f in FACTOR_NAMES}

        for asset, pos in positions.items():
            if pos.quantity < 1e-9:
                continue
            price = prices.get(asset, pos.avg_entry)
            value = pos.quantity * price
            total_value += value
            exp = self.fm.factor_exposures(asset)
            for fname in FACTOR_NAMES:
                port_exposures[fname] += exp.get(fname, 0.0) * value

        if total_value > 1e-9:
            for fname in FACTOR_NAMES:
                port_exposures[fname] /= total_value

        cov = self._factor_cov_matrix()

        # Variance contribution: w_i * sum_j(w_j * cov_ij)
        factor_var_contrib: dict[str, float] = {}
        total_var = 0.0
        for f1 in FACTOR_NAMES:
            contrib = 0.0
            for f2 in FACTOR_NAMES:
                contrib += port_exposures[f1] * port_exposures[f2] * cov[f1].get(f2, 0.0)
            factor_var_contrib[f1] = contrib
            total_var += contrib

        return {
            "portfolio_exposures": port_exposures,
            "factor_variance_contribution": factor_var_contrib,
            "total_factor_variance": total_var,
            "total_factor_vol": math.sqrt(max(0.0, total_var)),
        }

    def factor_var(self, confidence: float = 0.95) -> float:
        """Factor-based Value-at-Risk estimate.

        Uses the normal approximation: VaR = z * sigma * portfolio_value.
        """
        # z-scores for common confidence levels
        z_table = {0.90: 1.282, 0.95: 1.645, 0.99: 2.326}
        z = z_table.get(confidence, 1.645)

        risk = self.portfolio_factor_risk()
        vol = risk["total_factor_vol"]
        equity = self.portfolio.total_equity()
        return z * vol * equity

    def concentration_risk(self) -> dict[str, Any]:
        """Identify if portfolio is over-exposed to any single factor.

        Returns per-factor exposure magnitude and flags factors where the
        portfolio weight exceeds 2 standard deviations of the cross-sectional
        mean exposure.
        """
        risk = self.portfolio_factor_risk()
        exposures = risk["portfolio_exposures"]
        vals = [abs(v) for v in exposures.values()]
        mu = _mean(vals) if vals else 0.0
        sigma = _stdev(vals, mu) if len(vals) >= 2 else 0.0
        threshold = mu + 2.0 * sigma if sigma > 1e-12 else mu * 2.0

        concentrated: dict[str, float] = {}
        for fname, exp in exposures.items():
            if abs(exp) > threshold and threshold > 1e-12:
                concentrated[fname] = exp

        return {
            "exposures": exposures,
            "mean_abs_exposure": mu,
            "threshold": threshold,
            "concentrated_factors": concentrated,
            "is_concentrated": len(concentrated) > 0,
        }


# ---------------------------------------------------------------------------
# factor_exposure_check — pluggable risk check for RiskAgent
# ---------------------------------------------------------------------------

def factor_exposure_check(factor_model: FactorModel, max_exposure: float = 3.0):
    """Risk check: reject trades that would create excessive single-factor exposure.

    Returns a check function compatible with ``RiskAgent(bus, name, check)``.
    The check inspects the trade's target asset and rejects if any factor
    loading exceeds *max_exposure* in absolute value.

    Args:
        factor_model: The FactorModel instance providing factor data.
        max_exposure: Maximum allowed absolute factor exposure for any
            single factor on the traded asset.
    """

    def check(intent: TradeIntent) -> tuple[bool, str]:
        # Determine the non-stablecoin asset being traded
        stables = {"USD", "USDC", "USDT", "DAI", "BUSD"}
        asset = intent.asset_out if intent.asset_in in stables else intent.asset_in
        if asset in stables:
            return True, "factor_check: stable pair"

        exposures = factor_model.factor_exposures(asset)
        violations: list[str] = []
        for fname, exp in exposures.items():
            if abs(exp) > max_exposure:
                violations.append(f"{fname}={exp:+.2f}")

        if violations:
            msg = f"factor_check: REJECT excessive exposure on {asset}: {', '.join(violations)} (limit={max_exposure})"
            log.warning(msg)
            return False, msg

        worst = max(exposures.items(), key=lambda kv: abs(kv[1]), default=("none", 0.0))
        return True, f"factor_check: {asset} ok (worst: {worst[0]}={worst[1]:+.2f})"

    return check
