"""ML-based signal agent — stdlib-only lightweight ML pipeline.

Implements a from-scratch gradient-boosted decision tree classifier
that generates trading signals from price features.  No numpy, sklearn,
or any external ML library — pure Python stdlib.
"""
from __future__ import annotations

import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .core import Bus, MarketSnapshot, Signal

log = logging.getLogger("swarm.ml")

# ---------------------------------------------------------------------------
# 1. Feature Engineering
# ---------------------------------------------------------------------------

class FeatureEngine:
    """Collects price ticks and derives technical features for one asset."""

    def __init__(self, asset: str, max_history: int = 2000) -> None:
        self.asset = asset
        self.prices: deque[float] = deque(maxlen=max_history)

    def push(self, price: float) -> None:
        self.prices.append(price)

    @property
    def n(self) -> int:
        return len(self.prices)

    # -- individual features ------------------------------------------------

    def returns(self, window: int) -> list[float]:
        """Log returns over the last *window* ticks."""
        if self.n < 2:
            return []
        start = max(0, self.n - window - 1)
        seg = list(self.prices)[start:]
        return [math.log(seg[i] / seg[i - 1]) if seg[i - 1] > 0 else 0.0
                for i in range(1, len(seg))]

    def sma_cross(self, fast: int = 10, slow: int = 30) -> float:
        """Fast SMA / slow SMA - 1.  Positive ⇒ fast above slow."""
        if self.n < slow:
            return 0.0
        prices = list(self.prices)
        fast_sma = sum(prices[-fast:]) / fast
        slow_sma = sum(prices[-slow:]) / slow
        return (fast_sma / slow_sma - 1.0) if slow_sma != 0 else 0.0

    def rsi(self, period: int = 14) -> float:
        """Relative Strength Index (0-100)."""
        rets = self.returns(period)
        if not rets:
            return 50.0
        gains = [r for r in rets if r > 0]
        losses = [-r for r in rets if r < 0]
        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 0.0
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    def volatility(self, window: int = 20) -> float:
        """Rolling standard deviation of log returns."""
        rets = self.returns(window)
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(var)

    def momentum(self, window: int = 10) -> float:
        """price[now] / price[now - window] - 1."""
        if self.n <= window:
            return 0.0
        prices = list(self.prices)
        old = prices[-window - 1]
        return (prices[-1] / old - 1.0) if old != 0 else 0.0

    def mean_reversion_z(self, window: int = 20) -> float:
        """Z-score of current price relative to rolling mean."""
        if self.n < window:
            return 0.0
        prices = list(self.prices)[-window:]
        mean = sum(prices) / len(prices)
        var = sum((p - mean) ** 2 for p in prices) / len(prices)
        std = math.sqrt(var) if var > 0 else 1e-9
        return (prices[-1] - mean) / std

    def volume_trend(self) -> float:
        """Volume data not available — stub returns 0."""
        return 0.0

    def price_acceleration(self) -> float:
        """Second derivative of price: return[-1] - return[-2]."""
        rets = self.returns(3)
        if len(rets) < 2:
            return 0.0
        return rets[-1] - rets[-2]

    def high_low_range(self, window: int = 20) -> float:
        """(max - min) / mean over window."""
        if self.n < window:
            return 0.0
        prices = list(self.prices)[-window:]
        hi, lo = max(prices), min(prices)
        mean = sum(prices) / len(prices)
        return (hi - lo) / mean if mean != 0 else 0.0

    def autocorrelation(self, lag: int = 1) -> float:
        """Lag-1 autocorrelation of returns."""
        rets = self.returns(40)
        if len(rets) < lag + 2:
            return 0.0
        n = len(rets) - lag
        mean = sum(rets) / len(rets)
        cov = sum((rets[i] - mean) * (rets[i + lag] - mean) for i in range(n)) / n
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return cov / var if var > 1e-15 else 0.0

    # -- composite ----------------------------------------------------------

    def feature_vector(self) -> list[float]:
        """All features as a flat list (10 features)."""
        return [
            self.sma_cross(10, 30),
            self.rsi(14),
            self.volatility(20),
            self.momentum(10),
            self.mean_reversion_z(20),
            self.volume_trend(),
            self.price_acceleration(),
            self.high_low_range(20),
            self.autocorrelation(1),
            self.momentum(5),
        ]

    @staticmethod
    def feature_names() -> list[str]:
        return [
            "sma_cross_10_30",
            "rsi_14",
            "volatility_20",
            "momentum_10",
            "mean_reversion_z_20",
            "volume_trend",
            "price_acceleration",
            "high_low_range_20",
            "autocorrelation_1",
            "momentum_5",
        ]


# ---------------------------------------------------------------------------
# 2. Decision Tree (from scratch, Gini impurity)
# ---------------------------------------------------------------------------

@dataclass
class _TreeNode:
    feature_idx: int | None = None
    threshold: float | None = None
    left: _TreeNode | None = None
    right: _TreeNode | None = None
    value: float | None = None  # leaf prediction (+1 or -1)
    count_pos: int = 0
    count_neg: int = 0


class DecisionTree:
    """Binary classification tree (1 = long, -1 = short), Gini criterion."""

    def __init__(self, max_depth: int = 5, min_samples_leaf: int = 10) -> None:
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.root: _TreeNode | None = None

    # -- public API ---------------------------------------------------------

    def fit(self, X: list[list[float]], y: list[float]) -> None:
        self.root = self._build(X, y, depth=0)

    def predict(self, x: list[float]) -> float:
        node = self.root
        if node is None:
            return 0.0
        while node.value is None:
            if x[node.feature_idx] <= node.threshold:  # type: ignore[index]
                node = node.left
            else:
                node = node.right
            if node is None:
                return 0.0
        return node.value

    def predict_proba(self, x: list[float]) -> float:
        """Return confidence 0-1 (fraction of majority class in leaf)."""
        node = self.root
        if node is None:
            return 0.5
        while node.value is None:
            if x[node.feature_idx] <= node.threshold:  # type: ignore[index]
                node = node.left
            else:
                node = node.right
            if node is None:
                return 0.5
        total = node.count_pos + node.count_neg
        if total == 0:
            return 0.5
        return max(node.count_pos, node.count_neg) / total

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _gini(y: list[float]) -> float:
        n = len(y)
        if n == 0:
            return 0.0
        pos = sum(1 for v in y if v > 0)
        neg = n - pos
        p_pos = pos / n
        p_neg = neg / n
        return 1.0 - p_pos ** 2 - p_neg ** 2

    def _build(self, X: list[list[float]], y: list[float], depth: int) -> _TreeNode:
        pos = sum(1 for v in y if v > 0)
        neg = len(y) - pos

        # Terminal conditions
        if (depth >= self.max_depth
                or len(y) < 2 * self.min_samples_leaf
                or pos == 0
                or neg == 0):
            val = 1.0 if pos >= neg else -1.0
            return _TreeNode(value=val, count_pos=pos, count_neg=neg)

        best_gain = -1.0
        best_feat = 0
        best_thresh = 0.0
        best_left_idx: list[int] = []
        best_right_idx: list[int] = []
        n = len(y)
        parent_gini = self._gini(y)
        n_features = len(X[0]) if X else 0

        for fi in range(n_features):
            vals = sorted(set(X[i][fi] for i in range(n)))
            if len(vals) < 2:
                continue
            # candidate thresholds: midpoints between sorted unique values
            thresholds = [(vals[i] + vals[i + 1]) / 2 for i in range(len(vals) - 1)]
            # Subsample thresholds if too many
            if len(thresholds) > 20:
                thresholds = random.sample(thresholds, 20)
            for t in thresholds:
                l_idx = [i for i in range(n) if X[i][fi] <= t]
                r_idx = [i for i in range(n) if X[i][fi] > t]
                if len(l_idx) < self.min_samples_leaf or len(r_idx) < self.min_samples_leaf:
                    continue
                l_y = [y[i] for i in l_idx]
                r_y = [y[i] for i in r_idx]
                gain = parent_gini - (len(l_y) / n) * self._gini(l_y) - (len(r_y) / n) * self._gini(r_y)
                if gain > best_gain:
                    best_gain = gain
                    best_feat = fi
                    best_thresh = t
                    best_left_idx = l_idx
                    best_right_idx = r_idx

        if best_gain <= 0:
            val = 1.0 if pos >= neg else -1.0
            return _TreeNode(value=val, count_pos=pos, count_neg=neg)

        left_X = [X[i] for i in best_left_idx]
        left_y = [y[i] for i in best_left_idx]
        right_X = [X[i] for i in best_right_idx]
        right_y = [y[i] for i in best_right_idx]

        return _TreeNode(
            feature_idx=best_feat,
            threshold=best_thresh,
            left=self._build(left_X, left_y, depth + 1),
            right=self._build(right_X, right_y, depth + 1),
        )


# ---------------------------------------------------------------------------
# 3. Gradient Booster (simplified gradient boosting)
# ---------------------------------------------------------------------------

class GradientBooster:
    """Ensemble of DecisionTrees via simplified gradient boosting."""

    def __init__(
        self,
        n_trees: int = 10,
        learning_rate: float = 0.1,
        max_depth: int = 5,
        min_samples_leaf: int = 10,
    ) -> None:
        self.n_trees = n_trees
        self.learning_rate = learning_rate
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.trees: list[tuple[DecisionTree, list[int]]] = []  # (tree, feature_indices)
        self.n_features: int = 0
        self._fitted = False

    def fit(self, X: list[list[float]], y: list[float]) -> None:
        """Sequential boosting: each tree fits residuals of the ensemble."""
        n = len(y)
        if n == 0:
            return
        self.n_features = len(X[0])
        n_sub = max(1, int(math.sqrt(self.n_features)))  # feature subsample
        self.trees = []

        # Raw predictions (accumulated)
        raw = [0.0] * n

        for _ in range(self.n_trees):
            # Residuals = sign of (y - sigmoid(raw)) approximation
            # For classification boosting: pseudo-residual = y_i - p_i  where p_i = sigmoid(raw_i)
            residuals = [y[i] - self._sigmoid(raw[i]) * 2 + 1 for i in range(n)]
            # Convert residuals to labels for the tree
            labels = [1.0 if r > 0 else -1.0 for r in residuals]

            # Subsample features
            feat_idx = sorted(random.sample(range(self.n_features), n_sub))
            X_sub = [[row[fi] for fi in feat_idx] for row in X]

            tree = DecisionTree(max_depth=self.max_depth, min_samples_leaf=self.min_samples_leaf)
            tree.fit(X_sub, labels)
            self.trees.append((tree, feat_idx))

            # Update raw predictions
            for i in range(n):
                pred = tree.predict([X[i][fi] for fi in feat_idx])
                raw[i] += self.learning_rate * pred

        self._fitted = True

    def predict(self, x: list[float]) -> float:
        """Sum of tree predictions; sign = direction, magnitude = strength."""
        if not self.trees:
            return 0.0
        total = 0.0
        for tree, feat_idx in self.trees:
            x_sub = [x[fi] for fi in feat_idx]
            total += self.learning_rate * tree.predict(x_sub)
        return total

    def predict_proba(self, x: list[float]) -> float:
        """Sigmoid of raw prediction → confidence in [0, 1]."""
        return self._sigmoid(self.predict(x))

    @staticmethod
    def _sigmoid(z: float) -> float:
        # Numerically stable sigmoid
        if z >= 0:
            return 1.0 / (1.0 + math.exp(-z))
        ez = math.exp(z)
        return ez / (1.0 + ez)

    @property
    def fitted(self) -> bool:
        return self._fitted


# ---------------------------------------------------------------------------
# 4. Walk-Forward Validator
# ---------------------------------------------------------------------------

class WalkForwardValidator:
    """Rolling walk-forward cross-validation (no look-ahead bias)."""

    @staticmethod
    def validate(
        X: list[list[float]],
        y: list[float],
        train_pct: float = 0.7,
        n_folds: int = 5,
    ) -> dict[str, Any]:
        n = len(y)
        if n < 20:
            return {"accuracy": 0.0, "avg_return": 0.0, "sharpe": 0.0, "fold_results": []}

        min_train = int(n * train_pct)
        remaining = n - min_train
        fold_size = max(1, remaining // n_folds)
        fold_results: list[dict[str, Any]] = []

        for fold in range(n_folds):
            test_start = min_train + fold * fold_size
            test_end = min(test_start + fold_size, n)
            if test_start >= n or test_end <= test_start:
                break

            train_X = X[:test_start]
            train_y = y[:test_start]
            test_X = X[test_start:test_end]
            test_y = y[test_start:test_end]

            model = GradientBooster(n_trees=5, learning_rate=0.1, max_depth=4, min_samples_leaf=5)
            model.fit(train_X, train_y)

            correct = 0
            returns: list[float] = []
            for i, tx in enumerate(test_X):
                pred = model.predict(tx)
                actual = test_y[i]
                pred_sign = 1.0 if pred > 0 else -1.0
                if pred_sign == actual:
                    correct += 1
                # Simulated return: sign alignment
                returns.append(pred_sign * actual)

            acc = correct / len(test_y) if test_y else 0.0
            avg_ret = sum(returns) / len(returns) if returns else 0.0
            std_ret = (
                math.sqrt(sum((r - avg_ret) ** 2 for r in returns) / len(returns))
                if len(returns) > 1 else 1.0
            )
            sharpe = avg_ret / std_ret if std_ret > 1e-9 else 0.0

            fold_results.append({
                "fold": fold,
                "accuracy": round(acc, 4),
                "avg_return": round(avg_ret, 4),
                "sharpe": round(sharpe, 4),
                "n_train": len(train_y),
                "n_test": len(test_y),
            })

        if not fold_results:
            return {"accuracy": 0.0, "avg_return": 0.0, "sharpe": 0.0, "fold_results": []}

        avg_acc = sum(f["accuracy"] for f in fold_results) / len(fold_results)
        avg_ret = sum(f["avg_return"] for f in fold_results) / len(fold_results)
        avg_sharpe = sum(f["sharpe"] for f in fold_results) / len(fold_results)

        return {
            "accuracy": round(avg_acc, 4),
            "avg_return": round(avg_ret, 4),
            "sharpe": round(avg_sharpe, 4),
            "fold_results": fold_results,
        }


# ---------------------------------------------------------------------------
# 5. ML Signal Agent
# ---------------------------------------------------------------------------

class MLSignalAgent:
    """Event-driven ML signal agent.

    Subscribes to ``market.snapshot``, accumulates features, auto-retrains
    a GradientBooster, and publishes ``signal.ml``.
    """

    def __init__(
        self,
        bus: Bus,
        asset: str,
        retrain_interval: int = 500,
        min_samples: int = 200,
        lookahead: int = 10,
    ) -> None:
        self.bus = bus
        self.asset = asset
        self.retrain_interval = retrain_interval
        self.min_samples = min_samples
        self.lookahead = lookahead

        self.engine = FeatureEngine(asset)
        self.model = GradientBooster()
        self.validator = WalkForwardValidator()

        # Storage for training data
        self._features: list[list[float]] = []
        self._prices_at: list[float] = []  # price at each tick (for labelling)
        self._tick: int = 0
        self._last_train_tick: int = 0

        # Out-of-sample tracking
        self._oos_correct: int = 0
        self._oos_total: int = 0
        self._wf_accuracy: float = 0.0

        bus.subscribe("market.snapshot", self._on_snapshot)
        log.info("MLSignalAgent initialised for %s (retrain=%d, min=%d, lookahead=%d)",
                 asset, retrain_interval, min_samples, lookahead)

    async def _on_snapshot(self, snap: MarketSnapshot) -> None:
        price = snap.prices.get(self.asset)
        if price is None:
            return

        self.engine.push(price)
        self._tick += 1

        # Need enough history for features
        if self.engine.n < 40:
            return

        fv = self.engine.feature_vector()
        self._features.append(fv)
        self._prices_at.append(price)

        # --- Label older samples that now have lookahead resolved ---
        n_samples = len(self._prices_at)

        # --- Retrain when due ---
        if (n_samples > self.min_samples + self.lookahead
                and self._tick - self._last_train_tick >= self.retrain_interval):
            self._train()

        # --- Predict & publish if model is fitted ---
        if self.model.fitted:
            pred = self.model.predict(fv)
            conf = self.model.predict_proba(fv)

            # OOS tracking: check if our last prediction was right
            # (simple: compare prediction sign with actual next-tick return)
            if self._oos_total > 0 and n_samples >= 2:
                last_ret = self._prices_at[-1] - self._prices_at[-2]
                last_pred_dir = 1.0 if pred > 0 else -1.0
                actual_dir = 1.0 if last_ret > 0 else -1.0
                if last_pred_dir == actual_dir:
                    self._oos_correct += 1
            self._oos_total += 1

            oos_acc = self._oos_correct / self._oos_total if self._oos_total > 0 else 0.0

            direction: str
            if pred > 0:
                direction = "long"
            elif pred < 0:
                direction = "short"
            else:
                direction = "flat"

            strength = max(-1.0, min(1.0, pred))
            n_feat = len(fv)
            rationale = (
                f"ml_boost: pred={pred:.3f} conf={conf:.3f} "
                f"features={n_feat} oos_acc={oos_acc:.3f}"
            )

            signal = Signal(
                agent_id="ml_signal",
                asset=self.asset,
                direction=direction,  # type: ignore[arg-type]
                strength=abs(strength),
                confidence=conf,
                rationale=rationale,
                ts=snap.ts,
            )
            await self.bus.publish("signal.ml", signal)

    def _train(self) -> None:
        """Build labelled dataset and fit the model."""
        n = len(self._prices_at)
        usable = n - self.lookahead
        if usable < self.min_samples:
            return

        X: list[list[float]] = []
        y: list[float] = []
        for i in range(usable):
            future_price = self._prices_at[i + self.lookahead]
            current_price = self._prices_at[i]
            if current_price == 0:
                continue
            label = 1.0 if future_price > current_price else -1.0
            X.append(self._features[i])
            y.append(label)

        if len(X) < self.min_samples:
            return

        log.info("MLSignalAgent training on %d samples (%d features)", len(X), len(X[0]))

        self.model.fit(X, y)
        self._last_train_tick = self._tick

        # Walk-forward validation
        wf = self.validator.validate(X, y)
        self._wf_accuracy = wf["accuracy"]
        log.info("Walk-forward accuracy=%.3f sharpe=%.3f folds=%d",
                 wf["accuracy"], wf["sharpe"], len(wf["fold_results"]))

    @property
    def wf_accuracy(self) -> float:
        return self._wf_accuracy

    @property
    def oos_accuracy(self) -> float:
        return self._oos_correct / self._oos_total if self._oos_total > 0 else 0.0


# ---------------------------------------------------------------------------
# 6. Risk check: ml_model_check
# ---------------------------------------------------------------------------

def ml_model_check(ml_agent: MLSignalAgent, min_accuracy: float = 0.52) -> bool:
    """Risk gate: reject if ML model's walk-forward accuracy is below threshold.

    Returns True if model passes (accuracy >= threshold), False otherwise.
    """
    acc = ml_agent.wf_accuracy
    if acc < min_accuracy:
        log.warning(
            "ML model check FAILED: walk-forward accuracy %.3f < %.3f threshold",
            acc, min_accuracy,
        )
        return False
    log.info("ML model check passed: accuracy %.3f >= %.3f", acc, min_accuracy)
    return True
