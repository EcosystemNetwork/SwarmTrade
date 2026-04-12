"""Strategist + risk + consensus coordinator with regime-aware adaptive weighting."""
from __future__ import annotations
import asyncio, time, logging
from .core import Bus, Signal, TradeIntent, RiskVerdict, PortfolioTracker, OrderSpec

log = logging.getLogger("swarm")


class Strategist:
    """Combines signals into intents with regime-aware adaptive weights and Kelly sizing.

    Features:
    - Regime-aware: adjusts weights based on market regime (trending/reverting/volatile)
    - Kelly criterion: sizes positions based on win rate and payoff ratio
    - Volatility damping: high vol reduces position sizes
    - Adaptive learning: nudges weights toward agents whose signals align with realized PnL
    - Cooldown: prevents overtrading
    """
    THRESHOLD = 0.20

    # Default weights for all signal sources
    DEFAULT_WEIGHTS = {
        "momentum": 0.18,
        "mean_rev": 0.12,
        "prism": 0.08,
        "orderbook": 0.08,
        "funding": 0.06,
        "prism_breakout": 0.06,
        # TA strategy agents
        "rsi": 0.10,
        "macd": 0.08,
        "bollinger": 0.07,
        "vwap": 0.05,
        "ichimoku": 0.05,
        "mtf": 0.04,
        "correlation": 0.03,
        "news": 0.06,
        "whale": 0.04,
        "confluence": 0.12,
        "position": 0.03,
        # Research-driven agents
        "liquidation": 0.08,
        "atr_stop": 0.06,
        # Market intelligence agents
        "open_interest": 0.07,
        "fear_greed": 0.05,
        "social": 0.04,
        "liquidation_levels": 0.06,
        "onchain": 0.04,
        "arbitrage": 0.04,
        # Citadel-grade quantitative agents
        "ml": 0.12,
        "hedge": 0.05,
        "rebalance": 0.04,
    }

    # Regime-specific weight overrides (applied as multipliers)
    REGIME_PROFILES = {
        "trending": {
            "momentum": 2.0, "mean_rev": 0.3, "prism": 1.0,
            "orderbook": 1.2, "funding": 0.8, "prism_breakout": 1.5,
            "rsi": 0.5, "macd": 2.0, "bollinger": 0.6,
            "vwap": 0.5, "ichimoku": 1.8, "mtf": 2.0, "correlation": 1.0,
            "news": 0.8, "whale": 1.0, "confluence": 1.5, "position": 1.0,
            "liquidation": 0.3, "atr_stop": 1.5,
            "open_interest": 1.5, "fear_greed": 0.5, "social": 0.5,
            "liquidation_levels": 0.5, "onchain": 0.8, "arbitrage": 1.0,
            "ml": 1.8, "hedge": 0.5, "rebalance": 0.3,
        },
        "mean_reverting": {
            "momentum": 0.3, "mean_rev": 2.5, "prism": 1.0,
            "orderbook": 1.5, "funding": 1.2, "prism_breakout": 0.5,
            "rsi": 2.0, "macd": 0.5, "bollinger": 2.0,
            "vwap": 2.0, "ichimoku": 0.5, "mtf": 0.3, "correlation": 1.5,
            "news": 1.2, "whale": 1.5, "confluence": 1.5, "position": 1.2,
            "liquidation": 2.5, "atr_stop": 1.0,
            "open_interest": 1.2, "fear_greed": 2.0, "social": 1.5,
            "liquidation_levels": 2.0, "onchain": 1.2, "arbitrage": 1.5,
            "ml": 1.5, "hedge": 1.5, "rebalance": 2.0,
        },
        "volatile": {
            "momentum": 0.5, "mean_rev": 0.5, "prism": 1.5,
            "orderbook": 0.8, "funding": 1.5, "prism_breakout": 0.3,
            "rsi": 1.2, "macd": 0.6, "bollinger": 1.5,
            "vwap": 1.0, "ichimoku": 0.4, "mtf": 0.3, "correlation": 0.5,
            "news": 1.5, "whale": 2.0, "confluence": 1.0, "position": 1.5,
            "liquidation": 2.0, "atr_stop": 1.8,
            "open_interest": 2.0, "fear_greed": 1.5, "social": 1.0,
            "liquidation_levels": 2.5, "onchain": 0.8, "arbitrage": 2.0,
            "ml": 1.2, "hedge": 2.0, "rebalance": 1.5,
        },
    }

    def __init__(self, bus: Bus, base_size: float = 500.0, ttl_s: float = 8.0,
                 cooldown_s: float = 2.0,
                 portfolio: "PortfolioTracker | None" = None):
        self.bus = bus
        self.base_size = base_size
        self.ttl_s = ttl_s
        self.cooldown_s = cooldown_s
        self.portfolio = portfolio
        self.weights = dict(self.DEFAULT_WEIGHTS)
        self.latest: dict[str, Signal] = {}
        self.vol_damp: float = 1.0
        self.regime: str = "unknown"
        self._cooldown_until: float = 0.0

        # Kelly criterion state
        self.wins = 0
        self.losses = 0
        self.total_win_pnl = 0.0
        self.total_loss_pnl = 0.0

        # Subscribe to all signal channels
        for topic in ("signal.momentum", "signal.mean_rev", "signal.prism",
                       "signal.orderbook", "signal.funding",
                       "signal.prism_volume", "signal.prism_breakout",
                       "signal.rsi", "signal.macd", "signal.bollinger",
                       "signal.vwap", "signal.ichimoku",
                       "signal.mtf", "signal.correlation",
                       "signal.news", "signal.whale",
                       "signal.confluence", "signal.position",
                       "signal.liquidation", "signal.atr_stop",
                       "signal.ml", "signal.hedge", "signal.rebalance"):
            bus.subscribe(topic, self._on_signal)
        bus.subscribe("signal.vol", self._on_vol)
        bus.subscribe("signal.spread", self._on_spread)
        bus.subscribe("signal.regime", self._on_regime)
        bus.subscribe("audit.attribution", self._on_attribution)

    async def _on_vol(self, sig: Signal):
        self.vol_damp = max(0.3, 1.0 - sig.confidence * 0.7)

    async def _on_spread(self, sig: Signal):
        # Spread affects confidence globally — wide spread = less confident
        spread_damp = sig.confidence  # already computed as confidence signal
        self.vol_damp = min(self.vol_damp, max(0.3, spread_damp))

    async def _on_regime(self, sig: Signal):
        old_regime = self.regime
        try:
            self.regime = sig.rationale.split("regime=")[1].split()[0]
        except (IndexError, ValueError):
            log.warning("Could not parse regime from rationale: %s", sig.rationale[:100])
        if self.regime != old_regime and self.regime in self.REGIME_PROFILES:
            self._apply_regime()
            log.info("REGIME SHIFT: %s -> %s", old_regime, self.regime)

    def _apply_regime(self):
        """Adjust weights based on detected market regime."""
        profile = self.REGIME_PROFILES.get(self.regime, {})
        base = dict(self.DEFAULT_WEIGHTS)
        for agent, mult in profile.items():
            if agent in base:
                base[agent] *= mult
        # Renormalize
        total = sum(base.values()) or 1.0
        self.weights = {k: v / total for k, v in base.items()}

    async def _on_signal(self, sig: Signal):
        self.latest[sig.agent_id] = sig
        await self._maybe_emit()

    async def _maybe_emit(self):
        now = time.time()
        if now < self._cooldown_until:
            return

        # Require core signals; optional ones contribute when available
        core = {"momentum", "mean_rev"}
        if not core.issubset(self.latest):
            return

        active = {a for a in self.weights if a in self.latest}
        total_w = sum(self.weights[a] for a in active) or 1.0
        score = sum(
            (self.weights[a] / total_w) * self.latest[a].strength * self.latest[a].confidence
            for a in active
        ) * self.vol_damp

        if abs(score) < self.THRESHOLD:
            return

        going_long = score > 0
        size = self._kelly_size(abs(score))

        # Spot trading: can only sell what we hold
        if not going_long and self.portfolio:
            # Determine the asset we'd be selling from the latest signal
            asset = next((s.asset for s in self.latest.values()), "ETH")
            held = self.portfolio.get(asset).quantity
            if held < 1e-9:
                return  # no position to sell — skip
            # Cap sell size to what we hold (in asset units * price)
            price = self.portfolio.last_prices.get(asset, 0)
            if price > 0:
                size = min(size, held * price * 0.95)  # sell up to 95% of position

        # Determine assets from the signal context
        asset = next((s.asset for s in self.latest.values()), "ETH")

        # Phase 10: Regime-adaptive order type selection
        order_spec = self._select_order_spec(abs(score), going_long)

        intent = TradeIntent.new(
            asset_in="USDC" if going_long else asset,
            asset_out=asset if going_long else "USDC",
            amount_in=size,
            min_out=0.0,
            ttl=now + self.ttl_s,
            supporting=[self.latest[a] for a in active],
            order_spec=order_spec,
        )
        self._cooldown_until = now + self.cooldown_s

        log.info("INTENT %s score=%+.3f damp=%.2f regime=%s kelly=%.2f type=%s %s->%s",
                 intent.id, score, self.vol_damp, self.regime,
                 size, order_spec.order_type if order_spec else "market",
                 intent.asset_in, intent.asset_out)
        await self.bus.publish("intent.new", intent)

    def _kelly_size(self, score: float) -> float:
        """Position sizing via fractional Kelly criterion."""
        if self.wins + self.losses < 5:
            # Not enough data — use score-proportional sizing
            return self.base_size * min(1.0, score)

        win_rate = self.wins / (self.wins + self.losses)
        avg_win = self.total_win_pnl / max(1, self.wins)
        avg_loss = abs(self.total_loss_pnl) / max(1, self.losses)

        if avg_loss < 1e-9:
            return self.base_size * min(1.0, score)

        payoff_ratio = avg_win / avg_loss
        if payoff_ratio < 1e-6:
            return self.base_size * min(1.0, score)
        # Kelly: f* = (p * b - q) / b where p=win_rate, q=1-p, b=payoff_ratio
        kelly_f = (win_rate * payoff_ratio - (1 - win_rate)) / payoff_ratio
        # Use quarter-Kelly for crypto safety (fat-tailed distributions)
        kelly_f = max(0.02, min(0.25, kelly_f * 0.25))

        size = self.base_size * kelly_f * min(1.0, score) / 0.125  # normalize
        return max(10.0, min(self.base_size * 1.5, size))  # clamp tighter

    def _select_order_spec(self, confidence: float, _going_long: bool) -> OrderSpec:
        """Select order type based on regime and signal confluence.

        Regime-adaptive logic:
        - Trending: trailing stops to ride momentum
        - Mean-reverting: limit orders at favorable prices (maker fees)
        - Volatile: tighter stops, smaller engagement
        - High confluence (>0.7): aggressive limit inside spread
        - Low confluence (<0.3): conservative market order, small size
        """
        spec = OrderSpec()

        if self.regime == "trending":
            spec.order_type = "market"
            if confidence > 0.5:
                spec.close_order_type = "trailing-stop"
                spec.close_price = 3.0  # 3% trailing offset
        elif self.regime == "reverting":
            spec.order_type = "limit"
            spec.post_only = True
            spec.time_in_force = "gtc"
        elif self.regime == "volatile":
            spec.order_type = "market"
            if confidence > 0.4:
                spec.close_order_type = "stop-loss"
                spec.close_price = 2.0  # tighter 2% stop
        else:
            if confidence > 0.6:
                spec.order_type = "limit"
                spec.post_only = True
            else:
                spec.order_type = "market"

        return spec

    async def _on_attribution(self, payload: dict):
        """Adaptive weighting: nudge weights toward profitable agents."""
        pnl = payload.get("pnl", 0.0)
        contribs: dict[str, float] = payload.get("contribs", {})

        # Track Kelly stats
        if pnl > 0:
            self.wins += 1
            self.total_win_pnl += pnl
        elif pnl < 0:
            self.losses += 1
            self.total_loss_pnl += pnl

        if pnl == 0 or not contribs:
            return
        for agent, contrib in contribs.items():
            if agent not in self.weights:
                continue
            aligned = (contrib > 0 and pnl > 0) or (contrib < 0 and pnl < 0)
            self.weights[agent] *= 1.05 if aligned else 0.97
        s = sum(self.weights.values()) or 1.0
        for k in self.weights:
            self.weights[k] /= s


# ---------- Risk ----------
class RiskAgent:
    def __init__(self, bus: Bus, name: str, check):
        self.bus, self.name, self.check = bus, name, check
        bus.subscribe("intent.new", self._on)

    async def _on(self, intent: TradeIntent):
        ok, reason = self.check(intent)
        await self.bus.publish("risk.verdict",
                               RiskVerdict(intent.id, self.name, ok, reason))


def size_check(max_size: float):
    def f(i: TradeIntent):
        return (i.amount_in <= max_size,
                f"amt={i.amount_in:.0f} cap={max_size:.0f}")
    return f


def allowlist_check(tokens: set[str]):
    def f(i: TradeIntent):
        ok = i.asset_in in tokens and i.asset_out in tokens
        return ok, f"in={i.asset_in} out={i.asset_out}"
    return f


def drawdown_check(state: dict, max_dd: float):
    def f(_i: TradeIntent):
        dd = state.get("daily_pnl", 0.0)
        return dd > -max_dd, f"daily_pnl={dd:.2f} limit={-max_dd:.2f}"
    return f


# ---------- Coordinator ----------
class Coordinator:
    """Collects risk verdicts and forwards approved intents to execution.

    - Unanimous consensus: ALL risk agents must approve
    - Verdict timeout: auto-rejects if not all verdicts arrive within timeout_s
    - Stale intent cleanup: purges old entries to prevent memory leaks
    """

    def __init__(self, bus: Bus, n_risk_agents: int, timeout_s: float = 5.0):
        self.bus = bus
        self.n = n_risk_agents
        self.timeout_s = timeout_s
        self.intents: dict[str, TradeIntent] = {}
        self.verdicts: dict[str, list[RiskVerdict]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        bus.subscribe("intent.new", self._on_intent)
        bus.subscribe("risk.verdict", self._on_verdict)

    async def _on_intent(self, intent: TradeIntent):
        self.intents[intent.id] = intent
        self.verdicts[intent.id] = []
        self._timers[intent.id] = asyncio.create_task(self._timeout(intent.id))

    async def _timeout(self, intent_id: str):
        """Auto-reject if not all verdicts arrive in time."""
        await asyncio.sleep(self.timeout_s)
        if intent_id in self.verdicts and len(self.verdicts[intent_id]) < self.n:
            got = len(self.verdicts.get(intent_id, []))
            log.warning("TIMEOUT %s got %d/%d verdicts — auto-rejecting",
                        intent_id, got, self.n)
            self._cleanup(intent_id)

    async def _on_verdict(self, v: RiskVerdict):
        bucket = self.verdicts.setdefault(v.intent_id, [])
        bucket.append(v)
        if len(bucket) < self.n:
            return
        # Cancel timeout timer
        timer = self._timers.pop(v.intent_id, None)
        if timer and not timer.done():
            timer.cancel()
        if all(x.approve for x in bucket):
            await self.bus.publish("exec.go", self.intents[v.intent_id])
        else:
            vetoes = "; ".join(f"{x.agent_id}:{x.reason}" for x in bucket if not x.approve)
            log.info("VETO %s %s", v.intent_id, vetoes)
        self._cleanup(v.intent_id)

    def _cleanup(self, intent_id: str):
        """Remove stale intent/verdict data."""
        self.intents.pop(intent_id, None)
        self.verdicts.pop(intent_id, None)
        timer = self._timers.pop(intent_id, None)
        if timer and not timer.done():
            timer.cancel()
