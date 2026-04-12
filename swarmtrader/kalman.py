"""Adaptive Kalman Filter — signal/noise separation for trading signals.

Inspired by KalmanGuard (ETHGlobal HackMoney 2026). Implements a Linear
Parameter-Varying (LPV) Kalman filter that adapts to market regime:

  - State vector: [price_level, velocity, acceleration] per asset
  - Measurement noise adapts to recent signal variance
  - Process noise adapts to regime (higher in volatile markets)
  - Posterior error covariance serves as a system-wide risk proxy

The filter sits between raw signal sources and the Strategist, removing
noise and outputting cleaned signals with confidence intervals.

Bus integration:
  Subscribes to: signal.* (all raw signals)
  Publishes to:  signal.filtered.* (cleaned signals with confidence)
                 risk.covariance (system-wide uncertainty metric)

No external dependencies — pure Python matrix math.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field

from .core import Bus, Signal

log = logging.getLogger("swarm.kalman")


@dataclass
class FilterState:
    """Kalman filter state for a single asset."""
    asset: str
    # State estimate: [level, velocity, acceleration]
    x: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    # Error covariance (3x3 diagonal approximation for speed)
    P: list[float] = field(default_factory=lambda: [1.0, 1.0, 1.0])
    # Adaptive noise parameters
    measurement_noise: float = 0.1
    process_noise: float = 0.01
    # History for noise estimation
    residuals: list[float] = field(default_factory=list)
    max_residuals: int = 50
    # Regime adaptation
    regime: str = "normal"
    # Tracking
    update_count: int = 0
    last_update: float = 0.0


# Process noise multipliers per regime
REGIME_NOISE = {
    "trending": 0.005,
    "mean_reverting": 0.02,
    "volatile": 0.05,
    "crisis": 0.1,
    "normal": 0.01,
}


class AdaptiveKalmanFilter:
    """Regime-adaptive Kalman filter for multi-asset signal cleaning.

    For each asset, maintains a 3-state model:
      x[0] = signal level (smoothed strength)
      x[1] = signal velocity (rate of change)
      x[2] = signal acceleration (momentum of change)

    The filter adapts measurement noise from observed residuals (innovation
    variance) and process noise from the current market regime.

    Usage:
      kf = AdaptiveKalmanFilter(bus)
      # Signals flow in via Bus, filtered signals come out
    """

    def __init__(self, bus: Bus, min_confidence_boost: float = 0.1):
        self.bus = bus
        self.min_confidence_boost = min_confidence_boost
        self._states: dict[str, FilterState] = {}
        self._regime: str = "normal"
        self._system_covariance: float = 1.0  # aggregate uncertainty

        # Subscribe to regime updates
        bus.subscribe("signal.regime", self._on_regime)

        # Subscribe to all signal topics for filtering
        signal_topics = [
            "signal.momentum", "signal.mean_reversion", "signal.volatility",
            "signal.rsi", "signal.macd", "signal.bollinger", "signal.vwap",
            "signal.ichimoku", "signal.whale", "signal.news",
            "signal.funding_rate", "signal.orderbook", "signal.onchain",
            "signal.fear_greed", "signal.social", "signal.ml",
            "signal.confluence", "signal.arbitrage", "signal.correlation",
            "signal.multitf", "signal.smart_money",
            "signal.exchange_flow", "signal.options",
        ]
        for topic in signal_topics:
            bus.subscribe(topic, self._on_signal)

    def _get_state(self, asset: str) -> FilterState:
        if asset not in self._states:
            self._states[asset] = FilterState(asset=asset)
        return self._states[asset]

    async def _on_regime(self, sig):
        if isinstance(sig, Signal):
            rationale = sig.rationale.lower()
            for regime in REGIME_NOISE:
                if regime in rationale:
                    self._regime = regime
                    break

    async def _on_signal(self, sig: Signal):
        """Filter an incoming signal through the Kalman filter."""
        if not isinstance(sig, Signal):
            return

        state = self._get_state(sig.asset)
        state.regime = self._regime

        # Convert signal to scalar measurement: direction * strength
        measurement = sig.strength if sig.direction == "long" else -sig.strength

        # --- Predict step ---
        dt = 1.0  # normalized time step
        # State transition: constant acceleration model
        # x_pred = F * x
        x_pred = [
            state.x[0] + state.x[1] * dt + 0.5 * state.x[2] * dt * dt,
            state.x[1] + state.x[2] * dt,
            state.x[2] * 0.95,  # acceleration decays (mean-reverting)
        ]

        # Process noise (regime-adaptive)
        q = REGIME_NOISE.get(state.regime, 0.01)
        P_pred = [
            state.P[0] + q * dt * dt,
            state.P[1] + q * dt,
            state.P[2] + q,
        ]

        # --- Update step ---
        # Measurement model: we observe x[0] (signal level)
        H = [1.0, 0.0, 0.0]
        R = state.measurement_noise

        # Innovation (residual)
        y = measurement - x_pred[0]

        # Innovation covariance
        S = P_pred[0] + R

        # Kalman gain (simplified for diagonal P)
        K = [P_pred[i] * H[i] / max(S, 1e-12) if i == 0 else 0.0 for i in range(3)]
        # For states 1,2 use cross-covariance approximation
        K[1] = P_pred[1] * 0.3 / max(S, 1e-12)
        K[2] = P_pred[2] * 0.1 / max(S, 1e-12)

        # State update
        state.x = [x_pred[i] + K[i] * y for i in range(3)]

        # Covariance update
        state.P = [P_pred[i] * (1 - K[i] * H[i]) if i == 0
                    else P_pred[i] * (1 - abs(K[i]) * 0.5)
                    for i in range(3)]
        # Ensure P stays positive
        state.P = [max(p, 1e-6) for p in state.P]

        # --- Adapt measurement noise from residuals ---
        state.residuals.append(y)
        if len(state.residuals) > state.max_residuals:
            state.residuals = state.residuals[-state.max_residuals:]

        if len(state.residuals) >= 5:
            # Variance of recent residuals
            mean_r = sum(state.residuals) / len(state.residuals)
            var_r = sum((r - mean_r) ** 2 for r in state.residuals) / len(state.residuals)
            # Adaptive R: innovation variance minus prediction uncertainty
            state.measurement_noise = max(0.01, var_r - state.P[0])

        state.update_count += 1
        state.last_update = time.time()

        # --- Compute confidence from posterior covariance ---
        # Lower covariance = higher confidence in the filtered signal
        uncertainty = math.sqrt(state.P[0])
        # Map uncertainty to confidence: high uncertainty -> low confidence
        confidence_from_filter = max(0.1, 1.0 - uncertainty)
        # Blend with original signal confidence
        filtered_confidence = min(1.0, (sig.confidence + confidence_from_filter) / 2 + self.min_confidence_boost)

        # Direction from filtered state
        filtered_strength = abs(state.x[0])
        filtered_direction = "long" if state.x[0] > 0 else "short" if state.x[0] < 0 else sig.direction

        # Velocity bonus: strong velocity in same direction boosts strength
        velocity_sign = 1 if state.x[1] > 0 else -1
        direction_sign = 1 if filtered_direction == "long" else -1
        if velocity_sign == direction_sign:
            filtered_strength = min(1.0, filtered_strength + abs(state.x[1]) * 0.2)

        # --- Publish filtered signal ---
        filtered_sig = Signal(
            agent_id=f"kalman_{sig.agent_id}",
            asset=sig.asset,
            direction=filtered_direction,
            strength=min(1.0, filtered_strength),
            confidence=filtered_confidence,
            rationale=(
                f"Kalman({sig.agent_id}): level={state.x[0]:+.3f} "
                f"vel={state.x[1]:+.3f} P={state.P[0]:.4f} "
                f"regime={state.regime} | {sig.rationale[:60]}"
            ),
        )
        await self.bus.publish(f"signal.filtered.{sig.agent_id}", filtered_sig)

        # --- Update system covariance (aggregate risk metric) ---
        all_P0 = [s.P[0] for s in self._states.values()]
        self._system_covariance = sum(all_P0) / max(len(all_P0), 1)
        if state.update_count % 20 == 0:
            await self.bus.publish("risk.covariance", {
                "system_covariance": round(self._system_covariance, 4),
                "asset_count": len(self._states),
                "regime": self._regime,
                "ts": time.time(),
            })

    def get_filtered_state(self, asset: str) -> dict | None:
        """Get current filtered state for an asset."""
        state = self._states.get(asset)
        if not state:
            return None
        return {
            "asset": asset,
            "level": round(state.x[0], 4),
            "velocity": round(state.x[1], 4),
            "acceleration": round(state.x[2], 4),
            "covariance": round(state.P[0], 4),
            "measurement_noise": round(state.measurement_noise, 4),
            "regime": state.regime,
            "updates": state.update_count,
        }

    def summary(self) -> dict:
        return {
            "assets_tracked": len(self._states),
            "system_covariance": round(self._system_covariance, 4),
            "regime": self._regime,
            "states": {
                asset: {
                    "level": round(s.x[0], 3),
                    "velocity": round(s.x[1], 3),
                    "P0": round(s.P[0], 4),
                    "updates": s.update_count,
                }
                for asset, s in self._states.items()
            },
        }
