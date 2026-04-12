"""Portfolio optimization engine — Markowitz, Risk Parity, Black-Litterman, and dynamic hedging."""
from __future__ import annotations
import asyncio, math, time, logging
from collections import deque
from dataclasses import dataclass, field
from .core import Bus, Signal, MarketSnapshot, TradeIntent, PortfolioTracker

log = logging.getLogger("swarm.portfolio")


# ---------------------------------------------------------------------------
# 1. CovarianceEstimator
# ---------------------------------------------------------------------------

class CovarianceEstimator:
    """Collects price returns from market snapshots and estimates covariance."""

    def __init__(self, bus: Bus, window: int = 200, halflife: float | None = None):
        self.bus = bus
        self.window = window
        self.halflife = halflife
        self._prices: dict[str, deque[float]] = {}   # asset -> deque of prices
        bus.subscribe("market.snapshot", self._on_snapshot)

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        for asset, price in snap.prices.items():
            self._prices.setdefault(asset, deque(maxlen=self.window + 1)).append(price)

    @property
    def assets(self) -> list[str]:
        """Assets with enough data for at least 2 returns."""
        return [a for a, p in self._prices.items() if len(p) >= 3]

    def returns_matrix(self) -> dict[str, list[float]]:
        """Compute log-returns for each asset over the rolling window."""
        result: dict[str, list[float]] = {}
        for asset in self.assets:
            prices = list(self._prices[asset])
            rets = []
            for i in range(1, len(prices)):
                if prices[i - 1] > 0:
                    rets.append(math.log(prices[i] / prices[i - 1]))
                else:
                    rets.append(0.0)
            result[asset] = rets
        return result

    def _exp_weights(self, n: int) -> list[float]:
        """Exponential decay weights for n observations."""
        if self.halflife is None or self.halflife <= 0:
            return [1.0] * n
        lam = math.log(2) / self.halflife
        raw = [math.exp(-lam * (n - 1 - i)) for i in range(n)]
        s = sum(raw)
        return [w / s for w in raw] if s > 0 else [1.0 / n] * n

    def covariance_matrix(self) -> dict[str, dict[str, float]]:
        """Compute (optionally exponentially weighted) covariance matrix."""
        rm = self.returns_matrix()
        assets = sorted(rm.keys())
        if not assets:
            return {}

        # align to shortest series
        min_len = min(len(rm[a]) for a in assets)
        if min_len < 2:
            return {a: {b: 0.0 for b in assets} for a in assets}

        aligned = {a: rm[a][-min_len:] for a in assets}
        weights = self._exp_weights(min_len)

        # weighted means
        means: dict[str, float] = {}
        for a in assets:
            means[a] = sum(w * r for w, r in zip(weights, aligned[a]))

        # weighted covariance
        cov: dict[str, dict[str, float]] = {}
        for a in assets:
            cov[a] = {}
            for b in assets:
                c = sum(
                    weights[k] * (aligned[a][k] - means[a]) * (aligned[b][k] - means[b])
                    for k in range(min_len)
                )
                # bias correction
                if self.halflife is None:
                    c *= min_len / (min_len - 1)
                else:
                    # Approximate bias correction for weighted case
                    sum_w = sum(weights)
                    sum_w2 = sum(w * w for w in weights)
                    denom = sum_w * sum_w - sum_w2
                    if denom > 1e-18:
                        c *= (sum_w * sum_w) / denom
                cov[a][b] = c
        return cov

    def correlation_matrix(self) -> dict[str, dict[str, float]]:
        """Normalized covariance matrix."""
        cov = self.covariance_matrix()
        if not cov:
            return {}
        assets = sorted(cov.keys())
        stds = {a: math.sqrt(max(cov[a][a], 1e-18)) for a in assets}
        corr: dict[str, dict[str, float]] = {}
        for a in assets:
            corr[a] = {}
            for b in assets:
                corr[a][b] = cov[a][b] / (stds[a] * stds[b])
        return corr


# ---------------------------------------------------------------------------
# 2. MarkowitzOptimizer
# ---------------------------------------------------------------------------

class MarkowitzOptimizer:
    """Mean-variance optimizer using projected gradient descent (no scipy)."""

    def __init__(self, cov_estimator: CovarianceEstimator, max_weight: float = 0.4,
                 lr: float = 0.01, max_iter: int = 2000, tol: float = 1e-8):
        self.cov = cov_estimator
        self.max_weight = max_weight
        self.lr = lr
        self.max_iter = max_iter
        self.tol = tol

    def _portfolio_variance(self, weights: list[float], assets: list[str],
                            cov: dict[str, dict[str, float]]) -> float:
        n = len(assets)
        var = 0.0
        for i in range(n):
            for j in range(n):
                var += weights[i] * weights[j] * cov[assets[i]][assets[j]]
        return var

    def _portfolio_return(self, weights: list[float], mu: list[float]) -> float:
        return sum(w * m for w, m in zip(weights, mu))

    def _project(self, weights: list[float]) -> list[float]:
        """Project weights onto the feasible set: sum=1, each in [0, max_weight]."""
        n = len(weights)
        # clamp to [0, max_weight]
        w = [max(0.0, min(self.max_weight, x)) for x in weights]
        # normalize to sum=1 iteratively
        for _ in range(50):
            s = sum(w)
            if s < 1e-12:
                w = [1.0 / n] * n
                break
            w = [x / s for x in w]
            w = [max(0.0, min(self.max_weight, x)) for x in w]
            if abs(sum(w) - 1.0) < 1e-10:
                break
        # final normalize
        s = sum(w)
        if s > 1e-12:
            w = [x / s for x in w]
        return w

    def _grad_variance(self, weights: list[float], assets: list[str],
                       cov: dict[str, dict[str, float]]) -> list[float]:
        """Gradient of portfolio variance w.r.t. weights: 2 * Sigma @ w."""
        n = len(assets)
        grad = [0.0] * n
        for i in range(n):
            for j in range(n):
                grad[i] += 2.0 * cov[assets[i]][assets[j]] * weights[j]
        return grad

    def optimize(self, target_return: float | None = None) -> dict[str, float]:
        """Minimum variance portfolio, or efficient frontier point if target_return given."""
        cov_mat = self.cov.covariance_matrix()
        if not cov_mat:
            return {}
        assets = sorted(cov_mat.keys())
        n = len(assets)
        if n == 0:
            return {}

        # expected returns from historical mean
        rm = self.cov.returns_matrix()
        mu = [sum(rm[a]) / len(rm[a]) if rm[a] else 0.0 for a in assets]

        # init equal weights
        w = self._project([1.0 / n] * n)

        # penalty weight for return constraint
        lam_ret = 10.0

        prev_var = float("inf")
        for it in range(self.max_iter):
            grad = self._grad_variance(w, assets, cov_mat)

            if target_return is not None:
                # add gradient of penalty: lam * (mu'w - target)^2
                port_ret = self._portfolio_return(w, mu)
                penalty_grad_scale = 2.0 * lam_ret * (port_ret - target_return)
                for i in range(n):
                    grad[i] += penalty_grad_scale * mu[i]

            # gradient step
            w = [w[i] - self.lr * grad[i] for i in range(n)]
            w = self._project(w)

            cur_var = self._portfolio_variance(w, assets, cov_mat)
            if abs(prev_var - cur_var) < self.tol:
                break
            prev_var = cur_var

        return {assets[i]: w[i] for i in range(n)}

    def efficient_frontier(self, n_points: int = 20) -> list[dict]:
        """Trace the efficient frontier."""
        rm = self.cov.returns_matrix()
        if not rm:
            return []
        assets = sorted(rm.keys())
        mu = [sum(rm[a]) / len(rm[a]) if rm[a] else 0.0 for a in assets]
        min_ret = min(mu)
        max_ret = max(mu)
        if abs(max_ret - min_ret) < 1e-12:
            max_ret = min_ret + 0.01

        cov_mat = self.cov.covariance_matrix()
        frontier = []
        for i in range(n_points):
            t = min_ret + (max_ret - min_ret) * i / max(n_points - 1, 1)
            weights = self.optimize(target_return=t)
            w_list = [weights.get(a, 0.0) for a in assets]
            port_var = self._portfolio_variance(w_list, assets, cov_mat)
            port_ret = self._portfolio_return(w_list, mu)
            frontier.append({
                "target_return": t,
                "portfolio_return": port_ret,
                "portfolio_risk": math.sqrt(max(port_var, 0.0)),
                "weights": weights,
            })
        return frontier


# ---------------------------------------------------------------------------
# 3. RiskParityOptimizer
# ---------------------------------------------------------------------------

class RiskParityOptimizer:
    """Equal risk contribution portfolio — each asset contributes equally to total variance."""

    def __init__(self, cov_estimator: CovarianceEstimator,
                 max_iter: int = 1000, tol: float = 1e-8):
        self.cov = cov_estimator
        self.max_iter = max_iter
        self.tol = tol

    def optimize(self) -> dict[str, float]:
        cov_mat = self.cov.covariance_matrix()
        if not cov_mat:
            return {}
        assets = sorted(cov_mat.keys())
        n = len(assets)
        if n == 0:
            return {}

        # start with equal weights
        w = [1.0 / n] * n

        for _ in range(self.max_iter):
            # compute Sigma @ w
            sigma_w = [0.0] * n
            for i in range(n):
                for j in range(n):
                    sigma_w[i] += cov_mat[assets[i]][assets[j]] * w[j]

            port_var = sum(w[i] * sigma_w[i] for i in range(n))
            if port_var < 1e-18:
                break

            # marginal risk contribution: MRC_i = sigma_w[i] / sqrt(port_var)
            port_vol = math.sqrt(port_var)
            mrc = [sigma_w[i] / port_vol for i in range(n)]

            # risk contribution: RC_i = w_i * MRC_i
            rc = [w[i] * mrc[i] for i in range(n)]
            total_rc = sum(rc)
            if total_rc < 1e-18:
                break

            # target: each RC = total_rc / n
            target_rc = total_rc / n

            # new weights proportional to inverse MRC (bisection-style update)
            new_w = [0.0] * n
            for i in range(n):
                if mrc[i] > 1e-18:
                    new_w[i] = target_rc / mrc[i]
                else:
                    new_w[i] = w[i]

            # normalize
            s = sum(new_w)
            if s > 1e-12:
                new_w = [x / s for x in new_w]

            # check convergence
            delta = math.sqrt(sum((new_w[i] - w[i]) ** 2 for i in range(n)))
            w = new_w
            if delta < self.tol:
                break

        return {assets[i]: w[i] for i in range(n)}


# ---------------------------------------------------------------------------
# 4. BlackLittermanModel
# ---------------------------------------------------------------------------

class BlackLittermanModel:
    """Black-Litterman posterior returns and optimal weights."""

    def __init__(self, cov_estimator: CovarianceEstimator,
                 risk_aversion: float = 2.5, max_weight: float = 0.4):
        self.cov = cov_estimator
        self.risk_aversion = risk_aversion
        self.max_weight = max_weight

    def _invert_2x2(self, m: list[list[float]]) -> list[list[float]] | None:
        det = m[0][0] * m[1][1] - m[0][1] * m[1][0]
        if abs(det) < 1e-18:
            return None
        return [
            [m[1][1] / det, -m[0][1] / det],
            [-m[1][0] / det, m[0][0] / det],
        ]

    def _mat_inv(self, mat: list[list[float]]) -> list[list[float]]:
        """Invert a small matrix via Gauss-Jordan elimination."""
        n = len(mat)
        # augment with identity
        aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(mat)]

        for col in range(n):
            # partial pivot
            max_row = col
            for row in range(col + 1, n):
                if abs(aug[row][col]) > abs(aug[max_row][col]):
                    max_row = row
            aug[col], aug[max_row] = aug[max_row], aug[col]

            pivot = aug[col][col]
            if abs(pivot) < 1e-18:
                # singular — return identity as fallback
                return [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

            for j in range(2 * n):
                aug[col][j] /= pivot

            for row in range(n):
                if row == col:
                    continue
                factor = aug[row][col]
                for j in range(2 * n):
                    aug[row][j] -= factor * aug[col][j]

        return [row[n:] for row in aug]

    def _mat_mul(self, a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
        n = len(a)
        m = len(b[0])
        k = len(b)
        result = [[0.0] * m for _ in range(n)]
        for i in range(n):
            for j in range(m):
                for p in range(k):
                    result[i][j] += a[i][p] * b[p][j]
        return result

    def _mat_vec(self, mat: list[list[float]], vec: list[float]) -> list[float]:
        return [sum(mat[i][j] * vec[j] for j in range(len(vec))) for i in range(len(mat))]

    def _mat_add(self, a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
        return [[a[i][j] + b[i][j] for j in range(len(a[0]))] for i in range(len(a))]

    def _mat_scale(self, mat: list[list[float]], s: float) -> list[list[float]]:
        return [[mat[i][j] * s for j in range(len(mat[0]))] for i in range(len(mat))]

    def optimize(self, views: dict[str, float], tau: float = 0.05,
                 market_weights: dict[str, float] | None = None) -> dict[str, float]:
        """
        Combine market equilibrium with subjective views.
        views: {"BTC": 0.02, "ETH": -0.01} — expected excess returns.
        Returns optimal posterior weights.
        """
        cov_mat = self.cov.covariance_matrix()
        if not cov_mat:
            return {}
        assets = sorted(cov_mat.keys())
        n = len(assets)
        if n == 0:
            return {}

        # covariance as 2D list
        sigma = [[cov_mat[assets[i]][assets[j]] for j in range(n)] for i in range(n)]

        # market cap weights (default equal)
        if market_weights is None:
            w_mkt = [1.0 / n] * n
        else:
            total = sum(market_weights.get(a, 0.0) for a in assets)
            if total < 1e-12:
                w_mkt = [1.0 / n] * n
            else:
                w_mkt = [market_weights.get(a, 0.0) / total for a in assets]

        # implied equilibrium returns: pi = delta * Sigma @ w_mkt
        pi = [self.risk_aversion * sum(sigma[i][j] * w_mkt[j] for j in range(n))
              for i in range(n)]

        # build pick matrix P and view vector q from views
        view_assets = [a for a in assets if a in views]
        k = len(view_assets)
        if k == 0:
            # no views — return equilibrium weights
            return {assets[i]: w_mkt[i] for i in range(n)}

        P = [[0.0] * n for _ in range(k)]
        q = [0.0] * k
        for vi, a in enumerate(view_assets):
            idx = assets.index(a)
            P[vi][idx] = 1.0
            q[vi] = views[a]

        # view uncertainty: Omega = diag(P @ (tau*Sigma) @ P')
        tau_sigma = self._mat_scale(sigma, tau)
        P_tau_sigma = self._mat_mul(P, tau_sigma)
        Pt = [[P[j][i] for j in range(k)] for i in range(n)]
        omega_full = self._mat_mul(P_tau_sigma, Pt)
        omega = [[omega_full[i][j] if i == j else 0.0 for j in range(k)] for i in range(k)]

        # posterior: mu_bl = pi + tau*Sigma @ P' @ (P @ tau*Sigma @ P' + Omega)^-1 @ (q - P @ pi)
        inner = self._mat_add(self._mat_mul(P_tau_sigma, Pt), omega)
        inner_inv = self._mat_inv(inner)

        P_pi = self._mat_vec(P, pi)
        diff = [q[i] - P_pi[i] for i in range(k)]

        adj = self._mat_vec(inner_inv, diff)
        Pt_adj = self._mat_vec(Pt, adj)
        tau_sigma_Pt_adj = self._mat_vec(tau_sigma, [0.0] * n)
        # recompute properly
        step = self._mat_vec(self._mat_mul(tau_sigma, Pt), adj)

        mu_bl = [pi[i] + step[i] for i in range(n)]

        # optimal weights: w* = (delta * Sigma)^-1 @ mu_bl
        delta_sigma = self._mat_scale(sigma, self.risk_aversion)
        delta_sigma_inv = self._mat_inv(delta_sigma)
        w_opt = self._mat_vec(delta_sigma_inv, mu_bl)

        # project to feasible: long only, sum=1, max_weight
        w_opt = [max(0.0, min(self.max_weight, x)) for x in w_opt]
        s = sum(w_opt)
        if s > 1e-12:
            w_opt = [x / s for x in w_opt]

        return {assets[i]: w_opt[i] for i in range(n)}


# ---------------------------------------------------------------------------
# 5. DynamicHedger
# ---------------------------------------------------------------------------

class DynamicHedger:
    """Monitors portfolio beta to a primary asset and emits hedge signals."""

    def __init__(self, bus: Bus, cov_estimator: CovarianceEstimator,
                 portfolio: PortfolioTracker, primary_asset: str = "BTC",
                 target_beta: float = 0.0, hedge_threshold: float = 0.1):
        self.bus = bus
        self.cov = cov_estimator
        self.portfolio = portfolio
        self.primary_asset = primary_asset
        self.target_beta = target_beta
        self.hedge_threshold = hedge_threshold
        self._regime: str = "unknown"

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("signal.regime", self._on_regime)

    async def _on_regime(self, msg) -> None:
        if hasattr(msg, "rationale"):
            self._regime = msg.rationale
        elif isinstance(msg, dict):
            self._regime = msg.get("regime", "unknown")

    def _portfolio_beta(self) -> float:
        """Compute portfolio beta to the primary asset."""
        cov_mat = self.cov.covariance_matrix()
        if not cov_mat or self.primary_asset not in cov_mat:
            return 0.0

        assets = sorted(cov_mat.keys())
        primary_var = cov_mat[self.primary_asset][self.primary_asset]
        if primary_var < 1e-18:
            return 0.0

        # current portfolio weights by value
        total_val = 0.0
        positions: dict[str, float] = {}
        for asset in assets:
            pos = self.portfolio.positions.get(asset)
            if pos and pos.quantity > 1e-9:
                price = self.portfolio.last_prices.get(asset, pos.avg_entry)
                val = pos.quantity * price
                positions[asset] = val
                total_val += val

        if total_val < 1e-12:
            return 0.0

        # portfolio beta = sum(w_i * beta_i) where beta_i = cov(i, primary) / var(primary)
        beta = 0.0
        for asset, val in positions.items():
            w = val / total_val
            if asset in cov_mat and self.primary_asset in cov_mat[asset]:
                asset_beta = cov_mat[asset][self.primary_asset] / primary_var
                beta += w * asset_beta
        return beta

    def hedge_signal(self) -> Signal | None:
        """If portfolio beta deviates from target, emit a hedge signal."""
        beta = self._portfolio_beta()
        deviation = beta - self.target_beta

        if abs(deviation) < self.hedge_threshold:
            return None

        direction = "short" if deviation > 0 else "long"
        strength = min(abs(deviation), 1.0)

        return Signal(
            agent_id="dynamic_hedger",
            asset=self.primary_asset,
            direction=direction,
            strength=strength,
            confidence=min(0.5 + abs(deviation), 0.95),
            rationale=f"portfolio beta={beta:.3f} vs target={self.target_beta:.3f}, "
                      f"regime={self._regime}",
        )

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        sig = self.hedge_signal()
        if sig is not None:
            log.info("hedge signal: %s %s strength=%.2f (%s)",
                     sig.direction, sig.asset, sig.strength, sig.rationale)
            await self.bus.publish("signal.hedge", sig)


# ---------------------------------------------------------------------------
# 6. PortfolioOptAgent
# ---------------------------------------------------------------------------

class PortfolioOptAgent:
    """Ties together covariance estimation, optimization, and rebalancing."""

    OPTIMIZER_MAP = {
        "markowitz": "markowitz",
        "risk_parity": "risk_parity",
        "black_litterman": "black_litterman",
    }

    def __init__(self, bus: Bus, portfolio: PortfolioTracker,
                 optimizer: str = "markowitz",
                 rebalance_interval: float = 60.0,
                 max_weight: float = 0.4,
                 cov_window: int = 200,
                 halflife: float | None = None,
                 bl_views: dict[str, float] | None = None,
                 bl_tau: float = 0.05):
        self.bus = bus
        self.portfolio = portfolio
        self.optimizer_name = optimizer
        self.rebalance_interval = rebalance_interval
        self.bl_views = bl_views or {}
        self.bl_tau = bl_tau

        self.cov_estimator = CovarianceEstimator(bus, window=cov_window, halflife=halflife)
        self.markowitz = MarkowitzOptimizer(self.cov_estimator, max_weight=max_weight)
        self.risk_parity = RiskParityOptimizer(self.cov_estimator)
        self.black_litterman = BlackLittermanModel(self.cov_estimator, max_weight=max_weight)
        self.hedger: DynamicHedger | None = None

        self._target_weights: dict[str, float] = {}
        self._last_rebalance: float = 0.0
        self._running = False

        bus.subscribe("market.snapshot", self._on_snapshot)
        bus.subscribe("exec.report", self._on_exec_report)

    def attach_hedger(self, primary_asset: str = "BTC",
                      target_beta: float = 0.0) -> DynamicHedger:
        """Create and attach a DynamicHedger."""
        self.hedger = DynamicHedger(
            self.bus, self.cov_estimator, self.portfolio,
            primary_asset=primary_asset, target_beta=target_beta,
        )
        return self.hedger

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        self.portfolio.update_prices(snap.prices)

    async def _on_exec_report(self, report) -> None:
        # execution reports are handled by portfolio tracker elsewhere;
        # we just note that we may want to recheck allocation
        pass

    def current_weights(self) -> dict[str, float]:
        """Compute current portfolio weights from positions."""
        weights: dict[str, float] = {}
        total = 0.0
        for asset, pos in self.portfolio.positions.items():
            if pos.quantity > 1e-9:
                price = self.portfolio.last_prices.get(asset, pos.avg_entry)
                val = pos.quantity * price
                weights[asset] = val
                total += val
        if total > 1e-12:
            weights = {a: v / total for a, v in weights.items()}
        return weights

    def _run_optimizer(self) -> dict[str, float]:
        """Run the selected optimizer and return target weights."""
        if self.optimizer_name == "risk_parity":
            return self.risk_parity.optimize()
        elif self.optimizer_name == "black_litterman":
            mkt_w = self.current_weights() or None
            return self.black_litterman.optimize(
                views=self.bl_views, tau=self.bl_tau, market_weights=mkt_w,
            )
        else:
            return self.markowitz.optimize()

    async def rebalance(self) -> dict[str, float] | None:
        """Compute target weights and publish rebalance signal if needed."""
        target = self._run_optimizer()
        if not target:
            return None

        self._target_weights = target
        current = self.current_weights()

        # compute drift
        all_assets = set(target) | set(current)
        drifts = {
            a: target.get(a, 0.0) - current.get(a, 0.0)
            for a in all_assets
        }

        max_drift = max(abs(d) for d in drifts.values()) if drifts else 0.0
        if max_drift < 0.01:
            log.debug("portfolio drift %.4f within tolerance, skipping rebalance", max_drift)
            return target

        log.info("rebalance: optimizer=%s max_drift=%.4f targets=%s",
                 self.optimizer_name, max_drift,
                 {a: round(w, 4) for a, w in target.items()})

        await self.bus.publish("signal.rebalance", {
            "target_weights": target,
            "current_weights": current,
            "drifts": drifts,
            "optimizer": self.optimizer_name,
            "ts": time.time(),
        })
        self._last_rebalance = time.time()
        return target

    async def run(self) -> None:
        """Main loop — periodically rebalance."""
        self._running = True
        log.info("PortfolioOptAgent started (optimizer=%s, interval=%.0fs)",
                 self.optimizer_name, self.rebalance_interval)
        while self._running:
            try:
                await self.rebalance()
            except Exception:
                log.exception("rebalance error")
            await asyncio.sleep(self.rebalance_interval)

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# 7. rebalance_check — risk gate
# ---------------------------------------------------------------------------

def rebalance_check(opt_agent: PortfolioOptAgent, max_drift_pct: float = 0.10):
    """Risk check function: rejects trades that would increase drift from optimal allocation.

    Returns a coroutine-factory compatible with the Coordinator's risk-check pipeline.
    """

    async def _check(intent: TradeIntent) -> bool:
        target = opt_agent._target_weights
        if not target:
            return True  # no optimization yet, allow

        current = opt_agent.current_weights()

        # simulate the trade effect
        equity = opt_agent.portfolio.total_equity()
        if equity < 1e-12:
            return True

        # figure out which asset is being bought
        asset = intent.asset_out  # asset being acquired
        trade_value = intent.amount_in  # USD value being spent

        # current drift before trade
        drift_before = sum(
            abs(current.get(a, 0.0) - target.get(a, 0.0))
            for a in set(target) | set(current)
        )

        # simulated weights after trade
        sim_current = dict(current)
        # increase target asset weight
        if asset in sim_current:
            sim_current[asset] += trade_value / equity
        else:
            sim_current[asset] = trade_value / equity
        # decrease source asset weight
        if intent.asset_in in sim_current:
            sim_current[intent.asset_in] = max(
                0.0, sim_current[intent.asset_in] - trade_value / equity
            )

        # renormalize
        s = sum(sim_current.values())
        if s > 1e-12:
            sim_current = {a: v / s for a, v in sim_current.items()}

        drift_after = sum(
            abs(sim_current.get(a, 0.0) - target.get(a, 0.0))
            for a in set(target) | set(sim_current)
        )

        if drift_after > drift_before and drift_after > max_drift_pct:
            log.warning("rebalance_check REJECT: trade would increase drift %.4f -> %.4f "
                        "(max %.4f)", drift_before, drift_after, max_drift_pct)
            return False
        return True

    return _check
