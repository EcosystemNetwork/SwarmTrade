"""Technical analysis strategy agents: RSI, MACD, Bollinger Bands, VWAP, Ichimoku.

Each agent subscribes to market.snapshot, computes its indicator from a price
window, and publishes a typed Signal on the bus.  The Strategist blends these
with the other signal sources via its adaptive weighting system.
"""
from __future__ import annotations
import logging
from collections import deque
from .core import Bus, MarketSnapshot, Signal

log = logging.getLogger("swarm.strategies")


# ---------------------------------------------------------------------------
# RSI — Relative Strength Index
# ---------------------------------------------------------------------------
class RSIAgent:
    """Wilder RSI tuned for crypto volatility.

    Defaults: 7-period (faster than traditional 14), thresholds at 75/25
    (wider than 70/30) to reduce false signals in volatile crypto markets.

    Publishes signal.rsi with strength proportional to how far RSI deviates
    from the neutral 50 band, and confidence based on extremity.
    """

    name = "rsi"

    def __init__(self, bus: Bus, asset: str = "ETH", period: int = 7,
                 overbought: float = 75.0, oversold: float = 25.0):
        self.bus = bus
        self.asset = asset
        self.period = period
        self.overbought = overbought
        self.oversold = oversold
        self.prices: deque[float] = deque(maxlen=period + 1)
        self.avg_gain: float | None = None
        self.avg_loss: float | None = None
        bus.subscribe("market.snapshot", self._on_snap)

    async def _on_snap(self, snap: MarketSnapshot):
        price = snap.prices.get(self.asset)
        if price is None:
            return
        self.prices.append(price)
        if len(self.prices) <= self.period:
            return

        prices = list(self.prices)

        # First calculation — simple average
        if self.avg_gain is None:
            gains = [max(0, prices[i] - prices[i - 1]) for i in range(1, len(prices))]
            losses = [max(0, prices[i - 1] - prices[i]) for i in range(1, len(prices))]
            self.avg_gain = sum(gains) / self.period
            self.avg_loss = sum(losses) / self.period
        else:
            change = prices[-1] - prices[-2]
            gain = max(0, change)
            loss = max(0, -change)
            self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period

        if self.avg_loss < 1e-12:
            rsi = 100.0
        else:
            rs = self.avg_gain / self.avg_loss
            rsi = 100.0 - 100.0 / (1.0 + rs)

        # Map RSI relative to configurable thresholds
        # Above overbought → short bias, below oversold → long bias
        mid = (self.overbought + self.oversold) / 2.0
        half_range = (self.overbought - self.oversold) / 2.0
        deviation = (rsi - mid) / half_range  # -1 at oversold, +1 at overbought
        strength = max(-1.0, min(1.0, -deviation * 1.2))  # contrarian

        # Confidence: stronger when RSI breaches thresholds
        if rsi >= self.overbought or rsi <= self.oversold:
            confidence = min(1.0, abs(deviation) * 0.8 + 0.3)
        else:
            confidence = min(0.6, abs(deviation) * 0.6)

        if abs(strength) < 0.08:
            return

        sig = Signal(
            self.name, self.asset,
            "long" if strength > 0 else "short",
            strength, confidence,
            f"rsi={rsi:.1f}",
        )
        await self.bus.publish("signal.rsi", sig)


# ---------------------------------------------------------------------------
# MACD — Moving Average Convergence Divergence
# ---------------------------------------------------------------------------
class MACDAgent:
    """EMA-based MACD tuned for crypto pace (8/21/5 vs traditional 12/26/9).

    Faster periods catch crypto momentum shifts earlier. Crossovers of the
    MACD line over the signal line produce directional signals; histogram
    magnitude drives strength. Publishes signal.macd.
    """

    name = "macd"

    def __init__(self, bus: Bus, asset: str = "ETH",
                 fast: int = 8, slow: int = 21, signal_period: int = 5):
        self.bus = bus
        self.asset = asset
        self.fast_p = fast
        self.slow_p = slow
        self.signal_p = signal_period
        self._ema_fast: float | None = None
        self._ema_slow: float | None = None
        self._ema_signal: float | None = None
        self._prev_hist: float | None = None
        self._tick = 0
        bus.subscribe("market.snapshot", self._on_snap)

    @staticmethod
    def _ema_update(prev: float | None, price: float, period: int) -> float:
        if prev is None:
            return price
        k = 2.0 / (period + 1)
        return price * k + prev * (1 - k)

    async def _on_snap(self, snap: MarketSnapshot):
        price = snap.prices.get(self.asset)
        if price is None:
            return
        self._tick += 1
        self._ema_fast = self._ema_update(self._ema_fast, price, self.fast_p)
        self._ema_slow = self._ema_update(self._ema_slow, price, self.slow_p)

        if self._tick < self.slow_p:
            return

        macd_line = self._ema_fast - self._ema_slow
        self._ema_signal = self._ema_update(self._ema_signal, macd_line, self.signal_p)
        histogram = macd_line - self._ema_signal

        if self._tick < self.slow_p + self.signal_p:
            self._prev_hist = histogram
            return

        # Normalize histogram relative to price (makes it comparable across assets)
        norm_hist = histogram / price * 1000  # basis-point scale

        # Detect crossover for confidence boost
        crossed = False
        if self._prev_hist is not None:
            crossed = (histogram > 0) != (self._prev_hist > 0)
        self._prev_hist = histogram

        strength = max(-1.0, min(1.0, norm_hist * 5))
        confidence = 0.7 if crossed else min(0.8, abs(norm_hist) * 3 + 0.2)

        if abs(strength) < 0.05:
            return

        sig = Signal(
            self.name, self.asset,
            "long" if strength > 0 else "short",
            strength, confidence,
            f"macd={macd_line:.4f} sig={self._ema_signal:.4f} hist={histogram:.4f}"
            + (" CROSS" if crossed else ""),
        )
        await self.bus.publish("signal.macd", sig)


# ---------------------------------------------------------------------------
# Bollinger Bands — Squeeze & Breakout
# ---------------------------------------------------------------------------
class BollingerAgent:
    """Bollinger Bands with squeeze detection (2.5 std for crypto).

    Wider bands (2.5 vs traditional 2.0) reduce false signals in volatile
    crypto markets. Squeeze detection flags imminent volatility expansion.

    Publishes signal.bollinger.
    """

    name = "bollinger"

    def __init__(self, bus: Bus, asset: str = "ETH",
                 period: int = 20, num_std: float = 2.5):
        self.bus = bus
        self.asset = asset
        self.period = period
        self.num_std = num_std
        self.prices: deque[float] = deque(maxlen=period)
        self.bandwidth_history: deque[float] = deque(maxlen=50)
        bus.subscribe("market.snapshot", self._on_snap)

    async def _on_snap(self, snap: MarketSnapshot):
        price = snap.prices.get(self.asset)
        if price is None:
            return
        self.prices.append(price)
        if len(self.prices) < self.period:
            return

        prices = list(self.prices)
        sma = sum(prices) / len(prices)
        variance = sum((p - sma) ** 2 for p in prices) / len(prices)
        std = max(1e-9, variance ** 0.5)

        upper = sma + self.num_std * std
        lower = sma - self.num_std * std
        bandwidth = (upper - lower) / sma  # relative bandwidth

        self.bandwidth_history.append(bandwidth)

        # %B: where price sits within the bands (0 = lower, 1 = upper)
        band_range = upper - lower
        if band_range < 1e-9:
            return
        pct_b = (price - lower) / band_range

        # Squeeze detection: current bandwidth < 50th percentile of recent history
        squeeze = False
        if len(self.bandwidth_history) >= 20:
            sorted_bw = sorted(self.bandwidth_history)
            median_bw = sorted_bw[len(sorted_bw) // 2]
            squeeze = bandwidth < median_bw * 0.75

        # Mean-reversion bias: price at extremes pulls back
        # %B > 1 → overbought (short), %B < 0 → oversold (long)
        if pct_b > 1.0:
            strength = max(-1.0, -(pct_b - 1.0) * 2)
            direction = "short"
        elif pct_b < 0.0:
            strength = min(1.0, -pct_b * 2)
            direction = "long"
        else:
            # Within bands: mild mean-reversion toward middle
            strength = max(-1.0, min(1.0, -(pct_b - 0.5) * 0.8))
            direction = "long" if strength > 0 else "short"

        confidence = min(1.0, abs(pct_b - 0.5) * 1.5 + 0.2)
        if squeeze:
            confidence = min(1.0, confidence + 0.2)

        if abs(strength) < 0.05:
            return

        sig = Signal(
            self.name, self.asset,
            direction, strength, confidence,
            f"%B={pct_b:.3f} bw={bandwidth:.5f}" + (" SQUEEZE" if squeeze else ""),
        )
        await self.bus.publish("signal.bollinger", sig)


# ---------------------------------------------------------------------------
# VWAP — Volume-Weighted Average Price (price-only proxy)
# ---------------------------------------------------------------------------
class VWAPAgent:
    """Approximates VWAP anchored to weekly window (120-tick ≈ 240s at 2s poll).

    Wider anchor than daily VWAP since crypto trades 24/7 with no session
    boundaries. When price is far above VWAP → short bias; far below → long.

    Publishes signal.vwap.
    """

    name = "vwap"

    def __init__(self, bus: Bus, asset: str = "ETH", period: int = 120):
        self.bus = bus
        self.asset = asset
        self.period = period
        self.prices: deque[float] = deque(maxlen=period)
        bus.subscribe("market.snapshot", self._on_snap)

    async def _on_snap(self, snap: MarketSnapshot):
        price = snap.prices.get(self.asset)
        if price is None:
            return
        self.prices.append(price)
        if len(self.prices) < self.period:
            return

        prices = list(self.prices)
        n = len(prices)
        # Volume proxy: linearly increasing weights (more recent = more "volume")
        weights = list(range(1, n + 1))
        total_weight = sum(weights)
        vwap = sum(p * w for p, w in zip(prices, weights)) / total_weight

        # Deviation from VWAP as fraction of price
        if vwap < 1e-9:
            return
        deviation = (price - vwap) / vwap

        # Contrarian: overextended above VWAP → short, below → long
        strength = max(-1.0, min(1.0, -deviation * 60))

        # Confidence: higher when deviation is large and consistent
        recent_devs = [(p - vwap) / vwap for p in prices[-10:]]
        consistency = abs(sum(1 if d > 0 else -1 for d in recent_devs)) / len(recent_devs)
        confidence = min(1.0, abs(deviation) * 40 + consistency * 0.3)

        if abs(strength) < 0.05:
            return

        sig = Signal(
            self.name, self.asset,
            "long" if strength > 0 else "short",
            strength, confidence,
            f"vwap={vwap:.2f} dev={deviation:+.5f}",
        )
        await self.bus.publish("signal.vwap", sig)


# ---------------------------------------------------------------------------
# Ichimoku Cloud
# ---------------------------------------------------------------------------
class IchimokuAgent:
    """Simplified Ichimoku Kinko Hyo.

    Components computed from price highs/lows over windows:
    - Tenkan-sen (conversion): midpoint of 9-period high/low
    - Kijun-sen (base): midpoint of 26-period high/low
    - Senkou Span A (leading span A): avg of Tenkan + Kijun
    - Senkou Span B (leading span B): midpoint of 52-period high/low

    Signals:
    - Price above cloud → bullish; below → bearish
    - Tenkan/Kijun cross → momentum shift
    - Cloud thickness → trend strength

    Publishes signal.ichimoku.
    """

    name = "ichimoku"

    def __init__(self, bus: Bus, asset: str = "ETH",
                 tenkan: int = 9, kijun: int = 26, senkou_b: int = 52):
        self.bus = bus
        self.asset = asset
        self.tenkan_p = tenkan
        self.kijun_p = kijun
        self.senkou_b_p = senkou_b
        self.prices: deque[float] = deque(maxlen=senkou_b)
        self._prev_tenkan: float | None = None
        self._prev_kijun: float | None = None
        bus.subscribe("market.snapshot", self._on_snap)

    @staticmethod
    def _midpoint(prices: list[float], period: int) -> float:
        window = prices[-period:]
        return (max(window) + min(window)) / 2.0

    async def _on_snap(self, snap: MarketSnapshot):
        price = snap.prices.get(self.asset)
        if price is None:
            return
        self.prices.append(price)
        if len(self.prices) < self.senkou_b_p:
            return

        prices = list(self.prices)

        tenkan = self._midpoint(prices, self.tenkan_p)
        kijun = self._midpoint(prices, self.kijun_p)
        span_a = (tenkan + kijun) / 2.0
        span_b = self._midpoint(prices, self.senkou_b_p)

        cloud_top = max(span_a, span_b)
        cloud_bottom = min(span_a, span_b)
        cloud_thickness = (cloud_top - cloud_bottom) / price if price > 0 else 0

        # Price vs cloud
        if price > cloud_top:
            cloud_bias = min(1.0, (price - cloud_top) / cloud_top * 50)
        elif price < cloud_bottom:
            cloud_bias = max(-1.0, (price - cloud_bottom) / cloud_bottom * 50)
        else:
            # Inside the cloud — neutral / low conviction
            cloud_bias = 0.0

        # Tenkan/Kijun cross
        tk_cross_strength = 0.0
        if self._prev_tenkan is not None and self._prev_kijun is not None:
            prev_diff = self._prev_tenkan - self._prev_kijun
            curr_diff = tenkan - kijun
            if prev_diff <= 0 < curr_diff:
                tk_cross_strength = 0.5  # bullish cross
            elif prev_diff >= 0 > curr_diff:
                tk_cross_strength = -0.5  # bearish cross

        self._prev_tenkan = tenkan
        self._prev_kijun = kijun

        # Combine cloud position and TK cross
        strength = max(-1.0, min(1.0, cloud_bias * 0.7 + tk_cross_strength * 0.3))

        # Confidence: thick cloud + clear position = high confidence
        confidence = min(1.0, cloud_thickness * 200 + 0.3)
        if abs(cloud_bias) < 0.1:
            confidence *= 0.5  # inside cloud = low confidence

        if abs(strength) < 0.05:
            return

        parts = [f"tenkan={tenkan:.2f}", f"kijun={kijun:.2f}",
                 f"cloud={cloud_bottom:.2f}-{cloud_top:.2f}"]
        if abs(tk_cross_strength) > 0:
            parts.append("TK_CROSS")

        sig = Signal(
            self.name, self.asset,
            "long" if strength > 0 else "short",
            strength, confidence,
            " ".join(parts),
        )
        await self.bus.publish("signal.ichimoku", sig)


# ---------------------------------------------------------------------------
# Liquidation Cascade Detector
# ---------------------------------------------------------------------------
class LiquidationCascadeAgent:
    """Detects sharp liquidation cascades and fades the flush.

    Monitors price velocity and acceleration. When price drops/spikes >N%
    within a short window (cascade signature), emits a contrarian signal
    to fade the move after the flush stabilises.

    Research: OI drops >5% in minutes with violent price move = liquidation
    cascade. Mean-reversion after flush has high win rate.

    Publishes signal.liquidation.
    """

    name = "liquidation"

    def __init__(self, bus: Bus, asset: str = "ETH",
                 window: int = 15, cascade_pct: float = 0.02,
                 cooldown_ticks: int = 30):
        self.bus = bus
        self.asset = asset
        self.window = window
        self.cascade_pct = cascade_pct  # 2% move in window = cascade
        self.cooldown_ticks = cooldown_ticks
        self.prices: deque[float] = deque(maxlen=window + 1)
        self._cooldown = 0
        self._cascade_count = 0
        bus.subscribe("market.snapshot", self._on_snap)

    async def _on_snap(self, snap: MarketSnapshot):
        price = snap.prices.get(self.asset)
        if price is None:
            return
        self.prices.append(price)

        if self._cooldown > 0:
            self._cooldown -= 1
            return

        if len(self.prices) < self.window:
            return

        prices = list(self.prices)

        # Check for cascade: sharp move over window
        window_return = (prices[-1] / prices[-self.window]) - 1
        if abs(window_return) < self.cascade_pct:
            return

        # Confirm it's a cascade (not gradual): check acceleration
        # First half vs second half of the move
        mid = self.window // 2
        first_half = (prices[-mid] / prices[-self.window]) - 1
        second_half = (prices[-1] / prices[-mid]) - 1

        # Cascade = most of the move happened in one burst (not gradual drift)
        is_accelerating = abs(second_half) > abs(first_half) * 1.5
        is_decelerating = abs(first_half) > abs(second_half) * 1.5

        if not (is_accelerating or is_decelerating):
            return  # gradual move, not a cascade

        # Check for stabilisation: last 3 ticks should show deceleration
        if len(prices) >= 5:
            recent_move = abs((prices[-1] / prices[-3]) - 1)
            if recent_move > abs(window_return) * 0.5:
                return  # still cascading, wait for stabilisation

        # Fade the cascade (contrarian)
        strength = max(-1.0, min(1.0, -window_return * 20))

        # Higher confidence for larger cascades
        confidence = min(0.9, abs(window_return) * 15 + 0.3)

        self._cooldown = self.cooldown_ticks
        self._cascade_count += 1

        sig = Signal(
            self.name, self.asset,
            "long" if strength > 0 else "short",
            strength, confidence,
            f"cascade={window_return:+.4f} accel={'Y' if is_accelerating else 'N'}"
            f" count={self._cascade_count}",
        )
        await self.bus.publish("signal.liquidation", sig)


# ---------------------------------------------------------------------------
# ATR Trailing Stop Signal
# ---------------------------------------------------------------------------
class ATRTrailingStopAgent:
    """ATR-based dynamic trailing stop signal.

    Computes Average True Range and emits a signal when price violates the
    ATR trailing stop level. Research shows 1.5-3x ATR stops outperform
    fixed percentage stops in crypto.

    Also publishes the current ATR value as metadata for position managers
    to use for dynamic stop placement.

    Publishes signal.atr_stop.
    """

    name = "atr_stop"

    def __init__(self, bus: Bus, asset: str = "ETH",
                 period: int = 14, multiplier: float = 2.0):
        self.bus = bus
        self.asset = asset
        self.period = period
        self.multiplier = multiplier
        self.prices: deque[float] = deque(maxlen=period + 1)
        self._trailing_long: float | None = None  # trailing stop for longs
        self._trailing_short: float | None = None  # trailing stop for shorts
        self._trend: str = "flat"
        bus.subscribe("market.snapshot", self._on_snap)

    async def _on_snap(self, snap: MarketSnapshot):
        price = snap.prices.get(self.asset)
        if price is None:
            return
        self.prices.append(price)
        if len(self.prices) <= self.period:
            return

        prices = list(self.prices)

        # Compute ATR (using price-to-price as proxy for high-low-close)
        true_ranges = []
        for i in range(1, len(prices)):
            tr = abs(prices[i] - prices[i - 1])
            true_ranges.append(tr)

        atr = sum(true_ranges[-self.period:]) / self.period
        stop_distance = atr * self.multiplier

        # Update trailing stops
        long_stop = price - stop_distance
        short_stop = price + stop_distance

        if self._trailing_long is None:
            self._trailing_long = long_stop
            self._trailing_short = short_stop
        else:
            # Ratchet: only move stops in favorable direction
            self._trailing_long = max(self._trailing_long, long_stop)
            self._trailing_short = min(self._trailing_short, short_stop)

        # Detect stop violations
        prev_trend = self._trend
        if price <= self._trailing_long and self._trend != "short":
            self._trend = "short"
            self._trailing_short = short_stop  # reset opposite stop
        elif price >= self._trailing_short and self._trend != "long":
            self._trend = "long"
            self._trailing_long = long_stop  # reset opposite stop

        # Emit signal on trend changes or at extremes
        if self._trend == "flat":
            return

        # Distance from stop as fraction of ATR
        if atr < 1e-9:
            return
        if self._trend == "long":
            dist_from_stop = (price - self._trailing_long) / atr
        else:
            dist_from_stop = (self._trailing_short - price) / atr

        flipped = prev_trend != self._trend and prev_trend != "flat"

        strength_val = 1.0 if self._trend == "long" else -1.0
        # Dampen strength as price approaches stop
        strength_val *= min(1.0, dist_from_stop / self.multiplier)

        confidence = 0.8 if flipped else min(0.7, dist_from_stop / (self.multiplier * 2) + 0.2)

        if abs(strength_val) < 0.05:
            return

        sig = Signal(
            self.name, self.asset,
            self._trend,
            strength_val, confidence,
            f"atr={atr:.2f} stop_L={self._trailing_long:.2f}"
            f" stop_S={self._trailing_short:.2f}"
            + (" FLIP" if flipped else ""),
        )
        await self.bus.publish("signal.atr_stop", sig)


# ---------------------------------------------------------------------------
# Order Book Depth / Liquidity Check
# ---------------------------------------------------------------------------
def depth_liquidity_check(bus: Bus, min_depth_ratio: float = 2.0):
    """Risk check: reject trades when order book depth is insufficient.

    Requires the order book depth to be at least min_depth_ratio × the
    intended trade size. Prevents large slippage on illiquid books.

    Returns a check function compatible with RiskAgent.
    """
    _depth: dict[str, float] = {}

    async def _on_signal(sig):
        if sig.agent_id == "orderbook":
            # Extract total volume from rationale
            parts = sig.rationale.split()
            for p in parts:
                if p.startswith("bvol="):
                    bvol = float(p.split("=")[1])
                elif p.startswith("avol="):
                    avol = float(p.split("=")[1])
            _depth[sig.asset] = bvol + avol

    bus.subscribe("signal.orderbook", _on_signal)

    def check(intent):
        # Estimate the asset being traded
        asset = intent.asset_out if intent.asset_in in ("USD", "USDC", "USDT") else intent.asset_in
        available = _depth.get(asset, float("inf"))  # pass if no data yet
        needed = intent.amount_in * min_depth_ratio
        ok = available >= needed or available == float("inf")
        return ok, f"depth={available:.0f} needed={needed:.0f} ratio={min_depth_ratio}"

    return check
