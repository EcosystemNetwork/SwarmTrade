"""Strategist + risk + consensus coordinator."""
from __future__ import annotations
import time, logging
from .core import Bus, Signal, TradeIntent, RiskVerdict

log = logging.getLogger("swarm")


class Strategist:
    """Combines latest signals into intents with adaptive weights."""
    THRESHOLD = 0.20
    DEFAULT_WEIGHTS = {"momentum": 0.6, "mean_rev": 0.4}

    def __init__(self, bus: Bus, base_size: float = 1000.0, ttl_s: float = 8.0):
        self.bus = bus
        self.base_size = base_size
        self.ttl_s = ttl_s
        self.weights = dict(self.DEFAULT_WEIGHTS)
        self.latest: dict[str, Signal] = {}
        self.vol_damp: float = 1.0
        self._cooldown_until: float = 0.0
        bus.subscribe("signal.momentum", self._on_signal)
        bus.subscribe("signal.mean_rev", self._on_signal)
        bus.subscribe("signal.vol", self._on_vol)
        bus.subscribe("audit.attribution", self._on_attribution)

    async def _on_vol(self, sig: Signal):
        # high vol => stronger damping (toward 0.3)
        self.vol_damp = max(0.3, 1.0 - sig.confidence * 0.7)

    async def _on_signal(self, sig: Signal):
        self.latest[sig.agent_id] = sig
        await self._maybe_emit()

    async def _maybe_emit(self):
        now = time.time()
        if now < self._cooldown_until: return
        if not set(self.weights).issubset(self.latest): return
        score = sum(self.weights[a] * self.latest[a].strength * self.latest[a].confidence
                    for a in self.weights) * self.vol_damp
        if abs(score) < self.THRESHOLD:
            return
        going_long = score > 0
        intent = TradeIntent.new(
            asset_in="USDC" if going_long else "ETH",
            asset_out="ETH" if going_long else "USDC",
            amount_in=self.base_size * min(1.0, abs(score)),
            min_out=0.0,
            ttl=now + self.ttl_s,
            supporting=[self.latest[a] for a in self.weights],
        )
        self._cooldown_until = now + 2.0
        log.info("INTENT %s score=%+.3f damp=%.2f %s->%s amt=%.2f",
                 intent.id, score, self.vol_damp,
                 intent.asset_in, intent.asset_out, intent.amount_in)
        await self.bus.publish("intent.new", intent)

    async def _on_attribution(self, payload: dict):
        """Adaptive weighting: nudge weights toward agents whose signal direction
        matched the realized PnL sign. Bounded multiplicative update + renormalize."""
        pnl = payload.get("pnl", 0.0)
        contribs: dict[str, float] = payload.get("contribs", {})
        if pnl == 0 or not contribs: return
        for agent, contrib in contribs.items():
            if agent not in self.weights: continue
            aligned = (contrib > 0 and pnl > 0) or (contrib < 0 and pnl < 0)
            self.weights[agent] *= 1.05 if aligned else 0.97
        s = sum(self.weights.values()) or 1.0
        for k in self.weights: self.weights[k] /= s


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
        if len(bucket) < self.n: return
        if all(x.approve for x in bucket):
            await self.bus.publish("exec.go", self.intents[v.intent_id])
        else:
            vetoes = "; ".join(f"{x.agent_id}:{x.reason}" for x in bucket if not x.approve)
            log.info("VETO %s %s", v.intent_id, vetoes)
