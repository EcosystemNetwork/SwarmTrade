"""Strategist + risk + consensus coordinator with regime-aware adaptive weighting."""
from __future__ import annotations
import time, logging
from .core import Bus, Signal, TradeIntent, RiskVerdict

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
        "momentum": 0.30,
        "mean_rev": 0.20,
        "prism": 0.15,
        "orderbook": 0.15,
        "funding": 0.10,
        "prism_breakout": 0.10,
    }

    # Regime-specific weight overrides (applied as multipliers)
    REGIME_PROFILES = {
        "trending": {
            "momentum": 2.0, "mean_rev": 0.3, "prism": 1.0,
            "orderbook": 1.2, "funding": 0.8, "prism_breakout": 1.5,
        },
        "mean_reverting": {
            "momentum": 0.3, "mean_rev": 2.5, "prism": 1.0,
            "orderbook": 1.5, "funding": 1.2, "prism_breakout": 0.5,
        },
        "volatile": {
            "momentum": 0.5, "mean_rev": 0.5, "prism": 1.5,
            "orderbook": 0.8, "funding": 1.5, "prism_breakout": 0.3,
        },
    }

    def __init__(self, bus: Bus, base_size: float = 500.0, ttl_s: float = 8.0,
                 cooldown_s: float = 2.0):
        self.bus = bus
        self.base_size = base_size
        self.ttl_s = ttl_s
        self.cooldown_s = cooldown_s
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
                       "signal.prism_volume", "signal.prism_breakout"):
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
        self.regime = sig.rationale.split("regime=")[1].split()[0] if "regime=" in sig.rationale else "unknown"
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

        intent = TradeIntent.new(
            asset_in="USDC" if going_long else "ETH",
            asset_out="ETH" if going_long else "USDC",
            amount_in=size,
            min_out=0.0,
            ttl=now + self.ttl_s,
            supporting=[self.latest[a] for a in active],
        )
        self._cooldown_until = now + self.cooldown_s

        log.info("INTENT %s score=%+.3f damp=%.2f regime=%s kelly_size=%.2f %s->%s",
                 intent.id, score, self.vol_damp, self.regime,
                 size, intent.asset_in, intent.asset_out)
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
        # Kelly: f* = (p * b - q) / b where p=win_rate, q=1-p, b=payoff_ratio
        kelly_f = (win_rate * payoff_ratio - (1 - win_rate)) / payoff_ratio
        # Use half-Kelly for safety
        kelly_f = max(0.05, min(0.5, kelly_f * 0.5))

        size = self.base_size * kelly_f * min(1.0, score) / 0.25  # normalize
        return max(10.0, min(self.base_size * 2, size))  # clamp

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
    def __init__(self, bus: Bus, n_risk_agents: int):
        self.bus = bus
        self.n = n_risk_agents
        self.intents: dict[str, TradeIntent] = {}
        self.verdicts: dict[str, list[RiskVerdict]] = {}
        bus.subscribe("intent.new", self._on_intent)
        bus.subscribe("risk.verdict", self._on_verdict)

    async def _on_intent(self, intent: TradeIntent):
        self.intents[intent.id] = intent
        self.verdicts[intent.id] = []

    async def _on_verdict(self, v: RiskVerdict):
        bucket = self.verdicts.setdefault(v.intent_id, [])
        bucket.append(v)
        if len(bucket) < self.n:
            return
        if all(x.approve for x in bucket):
            await self.bus.publish("exec.go", self.intents[v.intent_id])
        else:
            vetoes = "; ".join(f"{x.agent_id}:{x.reason}" for x in bucket if not x.approve)
            log.info("VETO %s %s", v.intent_id, vetoes)
