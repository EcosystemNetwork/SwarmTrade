"""Advanced agents: order book imbalance, funding rates, spread, regime detection, sentiment."""
from __future__ import annotations
import asyncio, logging, math, os, time
from collections import deque
from .core import Bus, MarketSnapshot, Signal
from .kraken import _run_cli

log = logging.getLogger("swarm.agents")


# ---------------------------------------------------------------------------
# Order Book Imbalance Agent
# ---------------------------------------------------------------------------
class OrderBookAgent:
    """Computes bid/ask imbalance from L2 order book depth.
    Strong bid imbalance → bullish; strong ask imbalance → bearish."""

    name = "orderbook"

    def __init__(self, bus: Bus, pair: str = "ETHUSD", interval: float = 5.0):
        self.bus = bus
        self.pair = pair
        self.interval = interval
        self.asset = self._pair_to_asset(pair)
        self._stop = False

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("OrderBookAgent starting: pair=%s", self.pair)
        while not self._stop:
            try:
                data = await _run_cli("orderbook", self.pair)
                for pair_key, book in data.items():
                    asks = book.get("asks", [])
                    bids = book.get("bids", [])
                    if not asks or not bids:
                        continue

                    # Compute volume-weighted imbalance across all levels
                    bid_vol = sum(float(b[1]) for b in bids if len(b) >= 2)
                    ask_vol = sum(float(a[1]) for a in asks if len(a) >= 2)
                    total = bid_vol + ask_vol
                    if total < 1e-9:
                        continue

                    # Imbalance: +1 = all bids, -1 = all asks
                    imbalance = (bid_vol - ask_vol) / total

                    # Top-of-book pressure (more weight to near levels)
                    top_n = min(5, len(bids), len(asks))
                    top_bid = sum(float(bids[i][1]) for i in range(top_n))
                    top_ask = sum(float(asks[i][1]) for i in range(top_n))
                    top_total = top_bid + top_ask
                    top_imbalance = (top_bid - top_ask) / top_total if top_total > 0 else 0

                    # Blend full-book and top-of-book
                    strength = 0.4 * imbalance + 0.6 * top_imbalance
                    strength = max(-1.0, min(1.0, strength * 2))  # amplify
                    confidence = min(1.0, total / 100.0)  # higher volume = more confident

                    if abs(strength) > 0.05:
                        sig = Signal(
                            self.name, self.asset,
                            "long" if strength > 0 else "short",
                            strength, confidence,
                            f"imbalance={imbalance:+.3f} top={top_imbalance:+.3f} bvol={bid_vol:.1f} avol={ask_vol:.1f}",
                        )
                        await self.bus.publish(f"signal.{self.name}", sig)

            except (OSError, ValueError, KeyError, RuntimeError) as e:
                log.warning("OrderBookAgent error: %s", e)

            await asyncio.sleep(self.interval)

    @staticmethod
    def _pair_to_asset(pair: str) -> str:
        for q in ("USD", "USDT", "USDC"):
            if pair.upper().endswith(q):
                base = pair.upper()[:-len(q)]
                if base.startswith("X") and len(base) == 4:
                    base = base[1:]
                return {"XBT": "BTC"}.get(base, base)
        return pair


# ---------------------------------------------------------------------------
# Funding Rate Agent
# ---------------------------------------------------------------------------
class FundingRateAgent:
    """Monitors futures funding rates for contrarian signals.
    Extreme positive funding → shorts pay longs → market overleveraged long → bearish.
    Extreme negative funding → longs pay shorts → market overleveraged short → bullish."""

    name = "funding"

    def __init__(self, bus: Bus, symbol: str = "PF_ETHUSD", asset: str = "ETH",
                 interval: float = 60.0):
        self.bus = bus
        self.symbol = symbol
        self.asset = asset
        self.interval = interval
        self._stop = False
        self.history: deque[float] = deque(maxlen=24)  # last 24 readings

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("FundingRateAgent starting: symbol=%s", self.symbol)
        # Load historical funding rates for context
        try:
            data = await _run_cli("futures", "historical-funding-rates", self.symbol)
            rates = data.get("rates", [])
            for r in rates[-24:]:
                self.history.append(r.get("relativeFundingRate", 0))
        except Exception as e:
            log.warning("Failed to load funding history: %s", e)

        while not self._stop:
            try:
                data = await _run_cli("futures", "tickers")
                tickers = data.get("tickers", [])
                for t in tickers:
                    if t.get("symbol", "").upper() == self.symbol.upper():
                        rate = t.get("fundingRate", 0)
                        rel_rate = t.get("relativeFundingRate", rate)
                        if isinstance(rel_rate, (int, float)):
                            self.history.append(float(rel_rate))

                        if not self.history:
                            break

                        current = self.history[-1]
                        avg = sum(self.history) / len(self.history)
                        std = max(1e-12, (sum((r - avg)**2 for r in self.history) / len(self.history))**0.5)
                        z_score = (current - avg) / std

                        # Contrarian: extreme positive funding → bearish signal
                        strength = max(-1.0, min(1.0, -z_score / 2))
                        confidence = min(1.0, abs(z_score) / 3)

                        if abs(strength) > 0.05:
                            sig = Signal(
                                self.name, self.asset,
                                "long" if strength > 0 else "short",
                                strength, confidence,
                                f"funding={current:.8f} z={z_score:+.2f} avg={avg:.8f}",
                            )
                            await self.bus.publish(f"signal.{self.name}", sig)
                        break

            except (OSError, ValueError, KeyError, RuntimeError) as e:
                log.warning("FundingRateAgent error: %s", e)

            await asyncio.sleep(self.interval)


# ---------------------------------------------------------------------------
# Spread / Liquidity Agent
# ---------------------------------------------------------------------------
class SpreadAgent:
    """Monitors bid-ask spread as a liquidity/confidence signal.
    Widening spreads → uncertainty → reduce confidence.
    Tight spreads → liquid market → higher confidence."""

    name = "spread"

    def __init__(self, bus: Bus, pair: str = "ETHUSD", interval: float = 10.0):
        self.bus = bus
        self.pair = pair
        self.asset = OrderBookAgent._pair_to_asset(pair)
        self.interval = interval
        self._stop = False
        self.history: deque[float] = deque(maxlen=60)

    def stop(self):
        self._stop = True

    async def run(self):
        log.info("SpreadAgent starting: pair=%s", self.pair)
        while not self._stop:
            try:
                data = await _run_cli("ticker", self.pair)
                for pair_key, info in data.items():
                    ask = float(info["a"][0])
                    bid = float(info["b"][0])
                    mid = (ask + bid) / 2.0
                    spread_bps = ((ask - bid) / mid) * 10000

                    self.history.append(spread_bps)
                    if len(self.history) < 5:
                        continue

                    avg_spread = sum(self.history) / len(self.history)
                    spread_ratio = spread_bps / max(0.01, avg_spread)

                    # Spread widening = low confidence, tightening = high confidence
                    # This is a confidence-only signal (flat direction)
                    confidence = max(0.1, min(1.0, 2.0 - spread_ratio))

                    sig = Signal(
                        self.name, self.asset, "flat", 0.0, confidence,
                        f"spread={spread_bps:.2f}bps avg={avg_spread:.2f}bps ratio={spread_ratio:.2f}",
                    )
                    await self.bus.publish(f"signal.{self.name}", sig)

            except (OSError, ValueError, KeyError, RuntimeError) as e:
                log.warning("SpreadAgent error: %s", e)

            await asyncio.sleep(self.interval)


# ---------------------------------------------------------------------------
# Regime Detection Agent
# ---------------------------------------------------------------------------
class RegimeAgent:
    """Detects market regime: trending, mean-reverting, or volatile/choppy.
    Uses ADX-like directional movement and Hurst exponent approximation."""

    name = "regime"

    def __init__(self, bus: Bus, asset: str = "ETH", window: int = 50):
        self.bus = bus
        self.asset = asset
        self.window = window
        self.prices: deque[float] = deque(maxlen=window)
        self.regime: str = "unknown"  # trending, mean_reverting, volatile
        self.regime_strength: float = 0.0
        bus.subscribe("market.snapshot", self._on_snap)

    async def _on_snap(self, snap: MarketSnapshot):
        if self.asset not in snap.prices:
            return
        self.prices.append(snap.prices[self.asset])
        if len(self.prices) < self.window:
            return

        prices = list(self.prices)
        returns = [(prices[i] / prices[i-1]) - 1 for i in range(1, len(prices))]

        # --- ADX-like trend strength ---
        # Compute directional movement
        plus_dm = []
        minus_dm = []
        true_range = []
        for i in range(1, len(prices)):
            high_diff = prices[i] - prices[i-1]
            low_diff = prices[i-1] - prices[i]
            plus_dm.append(max(high_diff, 0))
            minus_dm.append(max(low_diff, 0))
            true_range.append(abs(prices[i] - prices[i-1]))

        n = len(plus_dm)
        if n < 14:
            return

        # Simple moving averages for DI
        period = 14
        avg_plus = sum(plus_dm[-period:]) / period
        avg_minus = sum(minus_dm[-period:]) / period
        avg_tr = sum(true_range[-period:]) / period

        if avg_tr < 1e-9:
            return

        plus_di = avg_plus / avg_tr
        minus_di = avg_minus / avg_tr
        di_sum = plus_di + minus_di
        dx = abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
        adx = dx  # simplified ADX

        # --- Hurst exponent approximation ---
        # R/S analysis over the window
        hurst = self._hurst_rs(returns)

        # --- Classify regime ---
        if adx > 0.3 and hurst > 0.55:
            self.regime = "trending"
            self.regime_strength = min(1.0, adx * hurst * 2)
        elif hurst < 0.45:
            self.regime = "mean_reverting"
            self.regime_strength = min(1.0, (0.5 - hurst) * 4)
        else:
            self.regime = "volatile"
            vol = (sum(r**2 for r in returns) / len(returns)) ** 0.5
            self.regime_strength = min(1.0, vol * 50)

        sig = Signal(
            self.name, self.asset, "flat", 0.0,
            self.regime_strength,
            f"regime={self.regime} adx={adx:.3f} hurst={hurst:.3f}",
        )
        await self.bus.publish("signal.regime", sig)

    @staticmethod
    def _hurst_rs(returns: list[float]) -> float:
        """Rescaled range (R/S) Hurst exponent estimate."""
        n = len(returns)
        if n < 20:
            return 0.5
        mean = sum(returns) / n
        deviations = [r - mean for r in returns]
        cumulative = []
        s = 0
        for d in deviations:
            s += d
            cumulative.append(s)
        r = max(cumulative) - min(cumulative)
        std = max(1e-12, (sum(d**2 for d in deviations) / n) ** 0.5)
        rs = r / std
        if rs <= 0:
            return 0.5
        hurst = math.log(rs) / math.log(n)
        return max(0.0, min(1.0, hurst))
