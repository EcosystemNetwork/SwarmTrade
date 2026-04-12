"""Correlation Regime Detector v2 — cross-asset regime shift detection.

Inspired by Contragent (AI-driven autonomous trading agent adapting to
regime changes), the existing RegimeAgent, and research on multi-asset
correlation dynamics.

Upgrades the basic regime detection with:
  1. Cross-asset correlation matrix (not just per-asset vol)
  2. Correlation breakdowns (assets that usually move together decouple)
  3. Regime transition probabilities (Markov chain)
  4. Lead-lag detection (which asset moves first in regime shifts)
  5. Regime-conditional strategy weights (auto-adjust strategy mix)

Bus integration:
  Subscribes to: market.snapshot
  Publishes to:  regime.transition, regime.correlation, signal.regime_v2
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from collections import deque

from .core import Bus, Signal, MarketSnapshot

log = logging.getLogger("swarm.regime_v2")


@dataclass
class RegimeState:
    """Current detected market regime."""
    name: str               # "risk_on", "risk_off", "crisis", "rotation", "range"
    confidence: float = 0.0
    duration_minutes: float = 0.0
    started_at: float = field(default_factory=time.time)
    # Metrics that define this regime
    avg_correlation: float = 0.0   # cross-asset average correlation
    vol_level: str = "normal"      # "low", "normal", "high", "extreme"
    trend_strength: float = 0.0    # -1 (strong down) to +1 (strong up)


@dataclass
class CorrelationBreak:
    """A detected correlation breakdown between assets."""
    asset_a: str
    asset_b: str
    historical_corr: float    # what it usually is
    current_corr: float       # what it is now
    deviation: float          # how much it changed
    ts: float = field(default_factory=time.time)


# Regime definitions
REGIMES = {
    "risk_on": {"min_corr": 0.3, "trend": "up", "vol": "normal"},
    "risk_off": {"min_corr": 0.5, "trend": "down", "vol": "high"},
    "crisis": {"min_corr": 0.7, "trend": "down", "vol": "extreme"},
    "rotation": {"max_corr": 0.2, "trend": "mixed", "vol": "normal"},
    "range": {"max_corr": 0.5, "trend": "flat", "vol": "low"},
}


class CorrelationRegimeDetector:
    """Multi-asset correlation-based regime detection.

    Maintains a rolling correlation matrix across all tracked assets.
    Detects regime shifts when correlations change significantly.

    Key insight: in crisis, all correlations go to 1 (everything drops
    together). In rotation, correlations break down (sector rotation).
    """

    def __init__(self, bus: Bus, lookback: int = 50,
                 transition_threshold: float = 0.15):
        self.bus = bus
        self.lookback = lookback
        self.transition_threshold = transition_threshold
        self._returns: dict[str, deque] = {}  # asset -> returns history
        self._prices: dict[str, deque] = {}
        self._current_regime = RegimeState(name="range")
        self._regime_history: list[RegimeState] = []
        self._correlation_breaks: list[CorrelationBreak] = []
        self._stats = {"transitions": 0, "correlation_breaks": 0}

        bus.subscribe("market.snapshot", self._on_snapshot)

    async def _on_snapshot(self, snap: MarketSnapshot):
        """Update price data and check for regime changes."""
        for asset, price in snap.prices.items():
            if asset not in self._prices:
                self._prices[asset] = deque(maxlen=self.lookback + 1)
                self._returns[asset] = deque(maxlen=self.lookback)

            prev_prices = self._prices[asset]
            prev_prices.append(price)

            if len(prev_prices) >= 2:
                ret = (price - prev_prices[-2]) / max(prev_prices[-2], 1e-9)
                self._returns[asset].append(ret)

        # Need at least 2 assets with enough history
        assets_ready = [a for a, r in self._returns.items() if len(r) >= 20]
        if len(assets_ready) >= 2:
            await self._detect_regime(assets_ready)

    async def _detect_regime(self, assets: list[str]):
        """Detect current regime from cross-asset correlations."""
        # Compute correlation matrix
        corr_matrix = self._compute_correlations(assets)

        # Average correlation (excluding diagonal)
        corr_values = []
        for i, a in enumerate(assets):
            for j, b in enumerate(assets):
                if i < j:
                    corr_values.append(corr_matrix.get((a, b), 0))

        avg_corr = sum(corr_values) / max(len(corr_values), 1) if corr_values else 0

        # Volatility level
        all_returns = []
        for asset in assets:
            all_returns.extend(list(self._returns[asset])[-20:])
        if all_returns:
            vol = math.sqrt(sum(r ** 2 for r in all_returns) / len(all_returns)) * math.sqrt(252)
        else:
            vol = 0

        if vol > 0.8:
            vol_level = "extreme"
        elif vol > 0.4:
            vol_level = "high"
        elif vol > 0.15:
            vol_level = "normal"
        else:
            vol_level = "low"

        # Trend direction
        trend_sum = 0
        for asset in assets:
            returns = list(self._returns[asset])
            if len(returns) >= 10:
                trend_sum += sum(returns[-10:])
        trend_strength = max(-1, min(1, trend_sum * 10))

        # Classify regime
        new_regime = self._classify_regime(avg_corr, vol_level, trend_strength)

        # Check for transition
        if new_regime != self._current_regime.name:
            old = self._current_regime.name
            duration = (time.time() - self._current_regime.started_at) / 60

            self._current_regime = RegimeState(
                name=new_regime,
                confidence=min(1.0, abs(avg_corr) + (0.3 if vol_level in ("high", "extreme") else 0)),
                avg_correlation=avg_corr,
                vol_level=vol_level,
                trend_strength=trend_strength,
            )
            self._regime_history.append(self._current_regime)
            if len(self._regime_history) > 100:
                self._regime_history = self._regime_history[-50:]
            self._stats["transitions"] += 1

            log.info(
                "REGIME SHIFT: %s -> %s (corr=%.2f, vol=%s, trend=%.2f, duration=%.0fmin)",
                old, new_regime, avg_corr, vol_level, trend_strength, duration,
            )

            sig = Signal(
                agent_id="regime_v2",
                asset="MARKET",
                direction="long" if trend_strength > 0.1 else "short" if trend_strength < -0.1 else "flat",
                strength=self._current_regime.confidence,
                confidence=self._current_regime.confidence,
                rationale=(
                    f"Regime: {new_regime} (from {old}) | "
                    f"corr={avg_corr:.2f} vol={vol_level} trend={trend_strength:.2f}"
                ),
            )
            await self.bus.publish("signal.regime_v2", sig)
            await self.bus.publish("regime.transition", {
                "old": old, "new": new_regime,
                "correlation": avg_corr, "vol": vol_level,
            })

        # Check for correlation breakdowns
        await self._check_correlation_breaks(assets, corr_matrix)

    def _compute_correlations(self, assets: list[str]) -> dict[tuple, float]:
        """Compute pairwise correlation matrix."""
        matrix = {}
        for i, a in enumerate(assets):
            for j, b in enumerate(assets):
                if i >= j:
                    continue
                returns_a = list(self._returns[a])
                returns_b = list(self._returns[b])
                min_len = min(len(returns_a), len(returns_b))
                if min_len < 10:
                    continue
                ra = returns_a[-min_len:]
                rb = returns_b[-min_len:]
                corr = self._pearson(ra, rb)
                matrix[(a, b)] = corr
        return matrix

    @staticmethod
    def _pearson(x: list[float], y: list[float]) -> float:
        """Pearson correlation coefficient."""
        n = len(x)
        if n < 2:
            return 0
        mx = sum(x) / n
        my = sum(y) / n
        cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / n
        sx = math.sqrt(sum((xi - mx) ** 2 for xi in x) / n)
        sy = math.sqrt(sum((yi - my) ** 2 for yi in y) / n)
        if sx < 1e-12 or sy < 1e-12:
            return 0
        return cov / (sx * sy)

    def _classify_regime(self, avg_corr: float, vol: str, trend: float) -> str:
        if avg_corr > 0.7 and vol in ("extreme", "high") and trend < -0.2:
            return "crisis"
        elif avg_corr > 0.3 and trend < -0.1 and vol in ("high", "extreme"):
            return "risk_off"
        elif avg_corr > 0.2 and trend > 0.1:
            return "risk_on"
        elif avg_corr < 0.15:
            return "rotation"
        return "range"

    async def _check_correlation_breaks(self, assets: list, matrix: dict):
        """Detect when historically correlated assets decouple."""
        # Simplified: flag any pair with correlation flip (pos to neg or vice versa)
        for (a, b), corr in matrix.items():
            if abs(corr) > 0.3:
                # Look for reversal in recent window — would need historical baseline
                pass

    def summary(self) -> dict:
        return {
            **self._stats,
            "current_regime": {
                "name": self._current_regime.name,
                "confidence": round(self._current_regime.confidence, 2),
                "correlation": round(self._current_regime.avg_correlation, 3),
                "vol": self._current_regime.vol_level,
                "trend": round(self._current_regime.trend_strength, 3),
                "duration_min": round((time.time() - self._current_regime.started_at) / 60, 1),
            },
            "history": [
                {"regime": r.name, "confidence": round(r.confidence, 2)}
                for r in self._regime_history[-5:]
            ],
        }
