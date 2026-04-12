"""Quantitative risk analytics — VaR, CVaR, and portfolio risk measures.

Uses only stdlib (math, statistics, random). No numpy/scipy dependency.
Subscribes to market snapshots and execution reports to maintain price
history and portfolio state for continuous risk measurement.
"""
from __future__ import annotations
import asyncio, logging, math, random, statistics, time
from collections import defaultdict
from dataclasses import dataclass, field
from .core import Bus, MarketSnapshot, ExecutionReport, PortfolioTracker, TradeIntent

log = logging.getLogger("swarm.var")


def _norm_ppf(p: float) -> float:
    """Approximate inverse normal CDF (percent-point function).

    Uses the rational approximation from Abramowitz & Stegun 26.2.23.
    Accurate to ~4.5e-4 for 0.01 < p < 0.99.
    """
    if p <= 0.0:
        return -6.0
    if p >= 1.0:
        return 6.0
    if p == 0.5:
        return 0.0
    if p > 0.5:
        return -_norm_ppf(1.0 - p)
    # Rational approx for 0 < p < 0.5
    t = math.sqrt(-2.0 * math.log(p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return -(t - (c0 + c1 * t + c2 * t * t) / (1.0 + d1 * t + d2 * t * t + d3 * t * t * t))


class VaREngine:
    """Calculates Value-at-Risk and related risk metrics.

    Subscribes to:
        - market.snapshot: collects price history for return calculations
        - exec.report: tracks portfolio state changes

    Publishes:
        - risk.var: periodic VaR/CVaR snapshot
    """

    def __init__(self, bus: Bus, portfolio: PortfolioTracker,
                 max_history: int = 500):
        self.bus = bus
        self.portfolio = portfolio
        self.max_history = max_history

        # price_history[asset] = list of prices in chronological order
        self.price_history: dict[str, list[float]] = defaultdict(list)
        # return_history[asset] = list of log returns
        self.return_history: dict[str, list[float]] = defaultdict(list)

        self._last_snapshot_ts: float = 0.0

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("exec.report", self._on_report)

    async def _on_snapshot(self, snap: MarketSnapshot):
        for asset, price in snap.prices.items():
            hist = self.price_history[asset]
            if hist:
                prev = hist[-1]
                if prev > 0:
                    lr = math.log(price / prev)
                    rets = self.return_history[asset]
                    rets.append(lr)
                    if len(rets) > self.max_history:
                        rets.pop(0)
            hist.append(price)
            if len(hist) > self.max_history:
                hist.pop(0)
        self._last_snapshot_ts = snap.ts

    async def _on_report(self, rep: ExecutionReport):
        # Portfolio state is updated by the execution layer; we just log
        if rep.status == "filled":
            log.debug("VaR engine noted fill: %s %s qty=%.6f",
                      rep.side, rep.asset, rep.quantity)

    # ── Single-asset VaR methods ──────────────────────────────────

    def _get_returns(self, asset: str, window: int | None = None) -> list[float]:
        rets = self.return_history.get(asset, [])
        if window is not None:
            rets = rets[-window:]
        return rets

    def historical_var(self, asset: str | None = None,
                       confidence: float = 0.95,
                       window: int = 100) -> float:
        """Historical simulation VaR for a single asset or the portfolio.

        Returns the loss amount in USD at the given confidence level.
        If asset is None, delegates to portfolio_var().
        """
        if asset is None:
            return self.portfolio_var(confidence=confidence)

        pos = self.portfolio.get(asset)
        if pos.quantity < 1e-9:
            return 0.0

        rets = self._get_returns(asset, window)
        if len(rets) < 5:
            return 0.0

        current_price = self.portfolio.last_prices.get(asset, pos.avg_entry)
        position_value = pos.quantity * current_price

        sorted_rets = sorted(rets)
        import math
        idx = max(0, min(len(sorted_rets) - 1, math.ceil((1.0 - confidence) * len(sorted_rets)) - 1))
        var_pct = -sorted_rets[idx]  # flip sign: loss is positive
        return max(0.0, position_value * var_pct)

    def parametric_var(self, asset: str | None = None,
                       confidence: float = 0.95) -> float:
        """Parametric (variance-covariance) VaR assuming normal distribution.

        Uses rolling mean and standard deviation of log returns.
        """
        if asset is None:
            return self.portfolio_var(confidence=confidence)

        pos = self.portfolio.get(asset)
        if pos.quantity < 1e-9:
            return 0.0

        rets = self._get_returns(asset)
        if len(rets) < 5:
            return 0.0

        mu = statistics.mean(rets)
        sigma = statistics.stdev(rets)
        if sigma < 1e-12:
            return 0.0

        z = _norm_ppf(1.0 - confidence)
        var_pct = -(mu + z * sigma)

        current_price = self.portfolio.last_prices.get(asset, pos.avg_entry)
        position_value = pos.quantity * current_price
        return max(0.0, position_value * var_pct)

    def monte_carlo_var(self, asset: str | None = None,
                        confidence: float = 0.95,
                        n_sims: int = 10000,
                        horizon: int = 1) -> float:
        """Monte Carlo VaR using Geometric Brownian Motion.

        Simulates `n_sims` random walks over `horizon` periods.
        """
        if asset is None:
            return self.portfolio_var(confidence=confidence)

        pos = self.portfolio.get(asset)
        if pos.quantity < 1e-9:
            return 0.0

        rets = self._get_returns(asset)
        if len(rets) < 5:
            return 0.0

        mu = statistics.mean(rets)
        sigma = statistics.stdev(rets)
        if sigma < 1e-12:
            return 0.0

        current_price = self.portfolio.last_prices.get(asset, pos.avg_entry)
        position_value = pos.quantity * current_price

        # Simulate terminal returns over horizon using GBM
        sim_returns: list[float] = []
        drift = mu - 0.5 * sigma * sigma  # GBM drift adjustment
        for _ in range(n_sims):
            total_return = 0.0
            for _ in range(horizon):
                z = random.gauss(0, 1)
                total_return += drift + sigma * z
            sim_returns.append(total_return)

        sim_returns.sort()
        idx = max(0, int((1.0 - confidence) * n_sims))
        var_pct = -sim_returns[idx]
        return max(0.0, position_value * var_pct)

    def cvar(self, asset: str | None = None,
             confidence: float = 0.95,
             window: int = 100) -> float:
        """Conditional VaR (Expected Shortfall).

        Mean of losses beyond the VaR threshold using historical returns.
        """
        if asset is None:
            # Portfolio-level CVaR
            assets = [a for a, p in self.portfolio.positions.items()
                      if p.quantity > 1e-9]
            if not assets:
                return 0.0
            # Use portfolio returns if we can compute them
            portfolio_losses = self._portfolio_loss_distribution(window)
            if len(portfolio_losses) < 5:
                return sum(self.cvar(a, confidence, window) for a in assets)
            sorted_losses = sorted(portfolio_losses, reverse=True)
            cutoff = max(1, int((1.0 - confidence) * len(sorted_losses)))
            tail = sorted_losses[:cutoff]
            return max(0.0, statistics.mean(tail)) if tail else 0.0

        pos = self.portfolio.get(asset)
        if pos.quantity < 1e-9:
            return 0.0

        rets = self._get_returns(asset, window)
        if len(rets) < 5:
            return 0.0

        current_price = self.portfolio.last_prices.get(asset, pos.avg_entry)
        position_value = pos.quantity * current_price

        sorted_rets = sorted(rets)
        cutoff = max(1, int((1.0 - confidence) * len(sorted_rets)))
        tail = sorted_rets[:cutoff]
        avg_tail = statistics.mean(tail)
        return max(0.0, position_value * (-avg_tail))

    # ── Portfolio-level VaR ───────────────────────────────────────

    def _portfolio_loss_distribution(self, window: int = 100) -> list[float]:
        """Compute historical portfolio loss distribution.

        For each time step, computes the weighted portfolio return using
        current position weights, then converts to dollar losses.
        """
        assets = [a for a, p in self.portfolio.positions.items()
                  if p.quantity > 1e-9]
        if not assets:
            return []

        # Get aligned returns (all assets must have returns for a given index)
        min_len = min(len(self._get_returns(a, window)) for a in assets)
        if min_len < 5:
            return []

        equity = self.portfolio.total_equity()
        if equity < 1e-9:
            return []

        # Compute position weights
        weights: dict[str, float] = {}
        for a in assets:
            price = self.portfolio.last_prices.get(a, self.portfolio.get(a).avg_entry)
            weights[a] = (self.portfolio.get(a).quantity * price) / equity

        # Compute portfolio returns for each time step
        losses: list[float] = []
        for i in range(min_len):
            port_ret = 0.0
            for a in assets:
                rets = self._get_returns(a, window)
                # Align from the end
                idx = len(rets) - min_len + i
                port_ret += weights[a] * rets[idx]
            losses.append(-port_ret * equity)  # positive = loss
        return losses

    def portfolio_var(self, confidence: float = 0.95) -> float:
        """Portfolio-level VaR using correlation across held assets.

        Uses variance-covariance approach with the correlation matrix.
        """
        assets = [a for a, p in self.portfolio.positions.items()
                  if p.quantity > 1e-9]
        if not assets:
            return 0.0

        if len(assets) == 1:
            return self.parametric_var(assets[0], confidence)

        equity = self.portfolio.total_equity()
        if equity < 1e-9:
            return 0.0

        # Position weights and individual volatilities
        weights: list[float] = []
        sigmas: list[float] = []
        valid_assets: list[str] = []

        for a in assets:
            rets = self._get_returns(a)
            if len(rets) < 5:
                continue
            price = self.portfolio.last_prices.get(a, self.portfolio.get(a).avg_entry)
            w = (self.portfolio.get(a).quantity * price) / equity
            weights.append(w)
            sigmas.append(statistics.stdev(rets))
            valid_assets.append(a)

        n = len(valid_assets)
        if n == 0:
            return 0.0

        # Build correlation matrix
        corr = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i == j:
                    corr[i][j] = 1.0
                else:
                    corr[i][j] = self._correlation(valid_assets[i], valid_assets[j])

        # Portfolio variance = w' * Sigma * w
        # where Sigma_ij = sigma_i * sigma_j * corr_ij
        port_var = 0.0
        for i in range(n):
            for j in range(n):
                port_var += (weights[i] * weights[j] *
                             sigmas[i] * sigmas[j] * corr[i][j])

        port_sigma = math.sqrt(max(0.0, port_var))
        z = _norm_ppf(1.0 - confidence)
        var_pct = -(z * port_sigma)  # z is negative for high confidence
        return max(0.0, equity * var_pct)

    def _correlation(self, asset_a: str, asset_b: str) -> float:
        """Pearson correlation between two assets' return series."""
        ra = self._get_returns(asset_a)
        rb = self._get_returns(asset_b)
        n = min(len(ra), len(rb))
        if n < 5:
            return 0.0  # assume uncorrelated if insufficient data

        # Align from the end
        ra = ra[-n:]
        rb = rb[-n:]

        mean_a = statistics.mean(ra)
        mean_b = statistics.mean(rb)

        cov = sum((ra[i] - mean_a) * (rb[i] - mean_b) for i in range(n)) / (n - 1)
        std_a = statistics.stdev(ra)
        std_b = statistics.stdev(rb)

        if std_a < 1e-12 or std_b < 1e-12:
            return 0.0
        return max(-1.0, min(1.0, cov / (std_a * std_b)))

    def marginal_var(self, asset: str, confidence: float = 0.95) -> float:
        """Marginal VaR: contribution of a single asset to portfolio VaR.

        Computed as portfolio VaR minus the VaR without this asset.
        """
        full_var = self.portfolio_var(confidence)

        pos = self.portfolio.get(asset)
        if pos.quantity < 1e-9:
            return 0.0

        # Temporarily zero out the position to compute VaR without it
        saved_qty = pos.quantity
        saved_cost = pos.cost_basis
        pos.quantity = 0.0
        pos.cost_basis = 0.0

        try:
            reduced_var = self.portfolio_var(confidence)
        finally:
            pos.quantity = saved_qty
            pos.cost_basis = saved_cost

        return max(0.0, full_var - reduced_var)

    # ── Hypothetical VaR (for risk checks) ────────────────────────

    def hypothetical_portfolio_var(self, asset: str, additional_usd: float,
                                   confidence: float = 0.95) -> float:
        """Estimate portfolio VaR if an additional position were added.

        Temporarily increases the position, computes VaR, then reverts.
        """
        pos = self.portfolio.get(asset)
        price = self.portfolio.last_prices.get(asset, pos.avg_entry)
        if price < 1e-9:
            return self.portfolio_var(confidence)

        additional_qty = additional_usd / price
        saved_qty = pos.quantity
        saved_cost = pos.cost_basis
        pos.quantity += additional_qty
        pos.cost_basis += additional_usd

        try:
            hyp_var = self.portfolio_var(confidence)
        finally:
            pos.quantity = saved_qty
            pos.cost_basis = saved_cost

        return hyp_var

    # ── Current metrics snapshot ──────────────────────────────────

    def current_metrics(self) -> dict:
        """Return a snapshot of all VaR metrics."""
        equity = self.portfolio.total_equity()
        p_var = self.portfolio_var()
        p_cvar = self.cvar()
        return {
            "portfolio_var_95": round(p_var, 2),
            "portfolio_cvar_95": round(p_cvar, 2),
            "portfolio_var_pct": round(p_var / equity * 100, 2) if equity > 0 else 0.0,
            "portfolio_cvar_pct": round(p_cvar / equity * 100, 2) if equity > 0 else 0.0,
            "equity": round(equity, 2),
            "per_asset": {
                a: {
                    "historical_var": round(self.historical_var(a), 2),
                    "parametric_var": round(self.parametric_var(a), 2),
                    "cvar": round(self.cvar(a), 2),
                    "marginal_var": round(self.marginal_var(a), 2),
                }
                for a, p in self.portfolio.positions.items()
                if p.quantity > 1e-9
            },
            "ts": time.time(),
        }

    # ── Periodic recalculation loop ──────────────────────────────

    async def run(self, interval: float = 30.0):
        """Recalculate and publish VaR metrics every `interval` seconds."""
        log.info("VaR engine started (interval=%.0fs)", interval)
        while True:
            await asyncio.sleep(interval)
            try:
                metrics = self.current_metrics()
                if metrics["equity"] > 0:
                    log.info("VaR: portfolio=%.2f (%.1f%%) CVaR=%.2f (%.1f%%)",
                             metrics["portfolio_var_95"],
                             metrics["portfolio_var_pct"],
                             metrics["portfolio_cvar_95"],
                             metrics["portfolio_cvar_pct"])
                    await self.bus.publish("risk.var", metrics)
            except Exception:
                log.exception("VaR calculation failed")


# ── Risk check function ──────────────────────────────────────────

def var_check(var_engine: VaREngine, max_var: float):
    """Risk check: reject trades that would push portfolio VaR above max_var.

    Args:
        var_engine: VaREngine instance with current market data.
        max_var: Maximum allowable portfolio VaR in USD.

    Returns:
        Closure compatible with RiskAgent check functions.
    """
    def f(intent: TradeIntent) -> tuple[bool, str]:
        # Determine the asset being acquired
        from .core import QUOTE_ASSETS
        if intent.asset_out in QUOTE_ASSETS:
            # Selling an asset — reduces risk, always ok
            return True, f"var_check: selling {intent.asset_in}, risk decreasing"

        asset = intent.asset_out
        hyp_var = var_engine.hypothetical_portfolio_var(asset, intent.amount_in)
        current_var = var_engine.portfolio_var()

        if hyp_var > max_var:
            return (False,
                    f"var_check: hypothetical VaR ${hyp_var:.2f} exceeds "
                    f"limit ${max_var:.2f} (current ${current_var:.2f})")
        return (True,
                f"var_check: hypothetical VaR ${hyp_var:.2f} within "
                f"limit ${max_var:.2f}")
    return f
