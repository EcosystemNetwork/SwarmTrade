"""Stress testing framework — scenario analysis and tail-risk measurement.

Uses only stdlib (math, statistics, random). No numpy/scipy dependency.
Works with PortfolioTracker to evaluate portfolio resilience under
historical and hypothetical stress scenarios.
"""
from __future__ import annotations
import logging, math, random, statistics, time
from dataclasses import dataclass, field
from .core import PortfolioTracker, TradeIntent, QUOTE_ASSETS

log = logging.getLogger("swarm.stress")


@dataclass
class StressScenario:
    """A stress scenario with price shocks per asset."""
    name: str
    description: str
    price_shocks: dict[str, float]  # asset -> pct change (e.g. -0.40 = -40%)
    duration_label: str = ""


# ── Predefined historical scenarios ──────────────────────────────

HISTORICAL_SCENARIOS: list[StressScenario] = [
    StressScenario(
        name="COVID Crash Mar 2020",
        description="Global pandemic panic sell-off across all risk assets",
        price_shocks={
            "BTC": -0.40, "ETH": -0.45, "SOL": -0.55, "XRP": -0.50,
            "ADA": -0.50, "DOT": -0.50, "LINK": -0.45, "AVAX": -0.50,
            "MATIC": -0.50, "DOGE": -0.45, "LTC": -0.45, "UNI": -0.50,
        },
        duration_label="1 week",
    ),
    StressScenario(
        name="FTX Collapse Nov 2022",
        description="Exchange insolvency contagion, SOL heavily exposed",
        price_shocks={
            "BTC": -0.25, "ETH": -0.30, "SOL": -0.60, "XRP": -0.30,
            "ADA": -0.35, "DOT": -0.35, "LINK": -0.30, "AVAX": -0.40,
            "MATIC": -0.35, "DOGE": -0.30, "LTC": -0.25, "UNI": -0.35,
        },
        duration_label="2 weeks",
    ),
    StressScenario(
        name="Luna/UST Depeg May 2022",
        description="Algorithmic stablecoin failure cascading through DeFi",
        price_shocks={
            "BTC": -0.30, "ETH": -0.35, "SOL": -0.40, "XRP": -0.35,
            "ADA": -0.40, "DOT": -0.40, "LINK": -0.35, "AVAX": -0.45,
            "MATIC": -0.40, "DOGE": -0.35, "LTC": -0.30, "UNI": -0.40,
        },
        duration_label="1 week",
    ),
    StressScenario(
        name="China Mining Ban Jun 2021",
        description="Chinese government bans crypto mining, hash rate crash",
        price_shocks={
            "BTC": -0.50, "ETH": -0.45, "SOL": -0.45, "XRP": -0.40,
            "ADA": -0.40, "DOT": -0.45, "LINK": -0.40, "AVAX": -0.45,
            "MATIC": -0.45, "DOGE": -0.50, "LTC": -0.50, "UNI": -0.40,
        },
        duration_label="1 month",
    ),
    StressScenario(
        name="Flash Crash Jan 2024",
        description="Rapid liquidation cascade in leveraged markets",
        price_shocks={
            "BTC": -0.15, "ETH": -0.20, "SOL": -0.25, "XRP": -0.20,
            "ADA": -0.22, "DOT": -0.22, "LINK": -0.18, "AVAX": -0.25,
            "MATIC": -0.22, "DOGE": -0.20, "LTC": -0.18, "UNI": -0.20,
        },
        duration_label="24 hours",
    ),
    StressScenario(
        name="Bull Run Reversal",
        description="Sharp correction after extended rally, profit-taking cascade",
        price_shocks={
            "BTC": -0.20, "ETH": -0.25, "SOL": -0.35, "XRP": -0.30,
            "ADA": -0.30, "DOT": -0.30, "LINK": -0.25, "AVAX": -0.35,
            "MATIC": -0.30, "DOGE": -0.35, "LTC": -0.25, "UNI": -0.30,
        },
        duration_label="1-2 weeks",
    ),
    StressScenario(
        name="Black Swan",
        description="Catastrophic tail event — all assets crash simultaneously",
        price_shocks={
            "BTC": -0.60, "ETH": -0.60, "SOL": -0.60, "XRP": -0.60,
            "ADA": -0.60, "DOT": -0.60, "LINK": -0.60, "AVAX": -0.60,
            "MATIC": -0.60, "DOGE": -0.60, "LTC": -0.60, "UNI": -0.60,
        },
        duration_label="unknown",
    ),
]


class StressTester:
    """Runs stress scenarios against the current portfolio.

    Takes a PortfolioTracker and optionally a VaREngine reference for
    combined tail-risk reporting.
    """

    def __init__(self, portfolio: PortfolioTracker,
                 var_engine=None,
                 survival_threshold: float = 0.50):
        """
        Args:
            portfolio: Live portfolio tracker with current positions.
            var_engine: Optional VaREngine for combined tail-risk reports.
            survival_threshold: Max portfolio loss fraction before
                                declaring non-survival (default 50%).
        """
        self.portfolio = portfolio
        self.var_engine = var_engine
        self.survival_threshold = survival_threshold

    def run_scenario(self, scenario: StressScenario) -> dict:
        """Apply a stress scenario to current positions.

        Returns:
            {scenario_name, portfolio_impact_usd, portfolio_impact_pct,
             per_asset_impact, survives}
        """
        equity = self.portfolio.total_equity()
        per_asset: dict[str, dict] = {}
        total_impact = 0.0

        for asset, pos in self.portfolio.positions.items():
            if pos.quantity < 1e-9:
                continue

            current_price = self.portfolio.last_prices.get(asset, pos.avg_entry)
            position_value = pos.quantity * current_price

            # Use asset-specific shock or default -60% for unlisted assets
            shock = scenario.price_shocks.get(asset, -0.60)
            impact_usd = position_value * shock
            total_impact += impact_usd

            per_asset[asset] = {
                "position_value": round(position_value, 2),
                "shock_pct": round(shock * 100, 1),
                "impact_usd": round(impact_usd, 2),
            }

        impact_pct = (total_impact / equity * 100) if equity > 0 else 0.0
        survives = abs(total_impact) < equity * self.survival_threshold

        return {
            "scenario_name": scenario.name,
            "description": scenario.description,
            "duration": scenario.duration_label,
            "portfolio_equity": round(equity, 2),
            "portfolio_impact_usd": round(total_impact, 2),
            "portfolio_impact_pct": round(impact_pct, 2),
            "per_asset_impact": per_asset,
            "survives": survives,
        }

    def run_all_scenarios(self) -> list[dict]:
        """Run all predefined historical stress scenarios.

        Returns list of scenario results sorted by impact (worst first).
        """
        results = [self.run_scenario(s) for s in HISTORICAL_SCENARIOS]
        results.sort(key=lambda r: r["portfolio_impact_usd"])
        log.info("Ran %d stress scenarios — worst: %s (%.1f%%)",
                 len(results),
                 results[0]["scenario_name"] if results else "n/a",
                 results[0]["portfolio_impact_pct"] if results else 0.0)
        return results

    def monte_carlo_stress(self, n_sims: int = 5000,
                           horizon_days: int = 5) -> dict:
        """Monte Carlo stress test using multivariate normal shocks.

        Generates correlated random shocks across all held assets and
        simulates portfolio impact over the given horizon.

        Returns:
            {p5_loss, p1_loss, mean_loss, worst_loss, survival_rate}
        """
        assets = [a for a, p in self.portfolio.positions.items()
                  if p.quantity > 1e-9]
        if not assets:
            return {"p5_loss": 0, "p1_loss": 0, "mean_loss": 0,
                    "worst_loss": 0, "survival_rate": 1.0}

        equity = self.portfolio.total_equity()
        if equity < 1e-9:
            return {"p5_loss": 0, "p1_loss": 0, "mean_loss": 0,
                    "worst_loss": 0, "survival_rate": 1.0}

        # Estimate per-asset daily vol from VaR engine or use defaults
        daily_vols: dict[str, float] = {}
        means: dict[str, float] = {}
        for a in assets:
            if (self.var_engine
                    and a in self.var_engine.return_history
                    and len(self.var_engine.return_history[a]) >= 5):
                rets = self.var_engine.return_history[a]
                daily_vols[a] = statistics.stdev(rets)
                means[a] = statistics.mean(rets)
            else:
                # Default crypto daily vol ~4%, slightly negative drift
                daily_vols[a] = 0.04
                means[a] = -0.001

        # Estimate pairwise correlations from VaR engine or default to 0.6
        n = len(assets)
        corr = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(n):
                if i == j:
                    corr[i][j] = 1.0
                elif self.var_engine:
                    corr[i][j] = self.var_engine._correlation(assets[i], assets[j])
                    if corr[i][j] == 0.0:
                        corr[i][j] = 0.6  # default correlation for crypto
                else:
                    corr[i][j] = 0.6

        # Cholesky decomposition for correlated random generation
        chol = self._cholesky(corr)

        # Run simulations
        losses: list[float] = []
        for _ in range(n_sims):
            # Generate independent standard normals
            z = [random.gauss(0, 1) for _ in range(n)]
            # Correlate them via Cholesky
            corr_z = [sum(chol[i][j] * z[j] for j in range(i + 1))
                      for i in range(n)]

            # Compute portfolio return over horizon
            port_loss = 0.0
            for idx, a in enumerate(assets):
                price = self.portfolio.last_prices.get(
                    a, self.portfolio.get(a).avg_entry)
                pos_val = self.portfolio.get(a).quantity * price
                # Multi-day shock: scale vol by sqrt(horizon)
                shock = (means[a] * horizon_days +
                         daily_vols[a] * math.sqrt(horizon_days) * corr_z[idx])
                port_loss += pos_val * (-shock)  # positive = loss
            losses.append(port_loss)

        losses.sort(reverse=True)  # worst losses first
        survival_count = sum(1 for l in losses
                             if l < equity * self.survival_threshold)

        return {
            "p5_loss": round(losses[int(0.05 * n_sims)], 2),
            "p1_loss": round(losses[int(0.01 * n_sims)], 2),
            "mean_loss": round(statistics.mean(losses), 2),
            "worst_loss": round(losses[0], 2),
            "survival_rate": round(survival_count / n_sims, 4),
            "n_sims": n_sims,
            "horizon_days": horizon_days,
        }

    @staticmethod
    def _cholesky(matrix: list[list[float]]) -> list[list[float]]:
        """Cholesky decomposition of a symmetric positive-definite matrix.

        Returns lower-triangular matrix L such that L * L^T = matrix.
        Falls back to diagonal if decomposition fails (non-PD matrix).
        """
        n = len(matrix)
        L = [[0.0] * n for _ in range(n)]
        try:
            for i in range(n):
                for j in range(i + 1):
                    s = sum(L[i][k] * L[j][k] for k in range(j))
                    if i == j:
                        val = matrix[i][i] - s
                        if val < 0:
                            raise ValueError("Matrix not positive definite")
                        L[i][j] = math.sqrt(val)
                    else:
                        if L[j][j] < 1e-12:
                            L[i][j] = 0.0
                        else:
                            L[i][j] = (matrix[i][j] - s) / L[j][j]
        except (ValueError, ZeroDivisionError):
            log.warning("Cholesky failed, falling back to diagonal (uncorrelated)")
            L = [[0.0] * n for _ in range(n)]
            for i in range(n):
                L[i][i] = math.sqrt(max(0.0, matrix[i][i]))
        return L

    def tail_risk_report(self) -> dict:
        """Combined tail-risk report: VaR + stress test + worst cases.

        Returns:
            {var_metrics, stress_results, monte_carlo, worst_scenarios,
             overall_risk_level}
        """
        # VaR metrics
        var_metrics = {}
        if self.var_engine:
            var_metrics = self.var_engine.current_metrics()

        # Historical stress scenarios
        stress_results = self.run_all_scenarios()

        # Monte Carlo stress
        mc = self.monte_carlo_stress()

        # Identify worst scenarios
        failing = [r for r in stress_results if not r["survives"]]
        worst_3 = stress_results[:3] if stress_results else []

        # Overall risk level
        equity = self.portfolio.total_equity()
        if equity < 1e-9:
            risk_level = "NONE"
        elif len(failing) >= 3:
            risk_level = "CRITICAL"
        elif len(failing) >= 1:
            risk_level = "HIGH"
        elif mc.get("survival_rate", 1.0) < 0.90:
            risk_level = "HIGH"
        elif mc.get("survival_rate", 1.0) < 0.95:
            risk_level = "MEDIUM"
        else:
            risk_level = "LOW"

        report = {
            "var_metrics": var_metrics,
            "stress_results": stress_results,
            "monte_carlo": mc,
            "worst_scenarios": worst_3,
            "failing_scenarios": [r["scenario_name"] for r in failing],
            "overall_risk_level": risk_level,
            "ts": time.time(),
        }

        log.info("Tail risk report: level=%s failing=%d/%d MC_survival=%.1f%%",
                 risk_level, len(failing), len(stress_results),
                 mc.get("survival_rate", 0) * 100)
        return report


# ── Risk check function ──────────────────────────────────────────

def stress_check(tester: StressTester, max_stress_loss_pct: float):
    """Risk check: reject trades if worst-case stress loss exceeds threshold.

    Runs the top-3 worst historical scenarios (by typical severity) and
    rejects the trade if any scenario would cause a loss exceeding
    max_stress_loss_pct of portfolio equity.

    Args:
        tester: StressTester instance with current portfolio.
        max_stress_loss_pct: Maximum acceptable loss as a percentage
                             (e.g. 40.0 for 40%).

    Returns:
        Closure compatible with RiskAgent check functions.
    """
    # Pre-select the 3 most severe scenarios by average shock magnitude
    top_scenarios = sorted(
        HISTORICAL_SCENARIOS,
        key=lambda s: statistics.mean(s.price_shocks.values()),
    )[:3]

    def f(intent: TradeIntent) -> tuple[bool, str]:
        # Selling reduces risk
        if intent.asset_out in QUOTE_ASSETS:
            return True, "stress_check: selling, risk decreasing"

        equity = tester.portfolio.total_equity()
        if equity < 1e-9:
            return True, "stress_check: no equity, pass"

        worst_pct = 0.0
        worst_name = ""
        for scenario in top_scenarios:
            result = tester.run_scenario(scenario)
            loss_pct = abs(result["portfolio_impact_pct"])
            if loss_pct > worst_pct:
                worst_pct = loss_pct
                worst_name = scenario.name

        if worst_pct > max_stress_loss_pct:
            return (False,
                    f"stress_check: worst scenario '{worst_name}' "
                    f"loss={worst_pct:.1f}% exceeds limit={max_stress_loss_pct:.1f}%")
        return (True,
                f"stress_check: worst scenario '{worst_name}' "
                f"loss={worst_pct:.1f}% within limit={max_stress_loss_pct:.1f}%")
    return f
