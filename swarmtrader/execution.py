"""Simulator, executor (dry-run safe), and SQLite auditor."""
from __future__ import annotations
import asyncio, logging, os, sqlite3, time, json
from pathlib import Path
from .core import Bus, TradeIntent, ExecutionReport, MarketSnapshot

log = logging.getLogger("swarm")


class Simulator:
    """Stand-in for forked-node simulation. Quotes against the latest mid price
    with synthetic slippage based on intent size, and sets min_out."""
    SLIPPAGE_BPS_PER_1K = 5  # 5 bps per $1k notional, toy model

    def __init__(self, bus: Bus):
        self.bus = bus
        self.last_price: float | None = None
        bus.subscribe("market.snapshot", self._on_snap)
        bus.subscribe("exec.go", self._on_go)

    async def _on_snap(self, snap: MarketSnapshot):
        self.last_price = snap.prices.get("ETH")

    async def _on_go(self, intent: TradeIntent):
        if self.last_price is None:
            await self.bus.publish("exec.simulated", (intent, None)); return
        bps = (intent.amount_in / 1000.0) * self.SLIPPAGE_BPS_PER_1K
        slip = bps / 10_000.0
        if intent.asset_in == "USDC":  # buying ETH
            eff = self.last_price * (1 + slip)
            out = intent.amount_in / eff
        else:                          # selling ETH
            eff = self.last_price * (1 - slip)
            out = intent.amount_in * eff
        intent.min_out = out * 0.995   # 50bps tolerance
        await self.bus.publish("exec.simulated", (intent, eff))


class Executor:
    """Dry-run executor. To go live: implement _submit with web3.py + a private
    relay (Flashbots/MEV-Share), KMS-backed signer, and reorg handling."""
    def __init__(self, bus: Bus, kill_switch: Path, dry_run: bool = True):
        self.bus, self.kill_switch, self.dry_run = bus, kill_switch, dry_run
        bus.subscribe("exec.simulated", self._on_sim)

    async def _on_sim(self, payload):
        intent, eff_price = payload
        if self.kill_switch.exists():
            await self._report(intent, "rejected", None, None, None, "kill_switch")
            return
        if eff_price is None:
            await self._report(intent, "rejected", None, None, None, "no_quote"); return
        if time.time() > intent.ttl:
            await self._report(intent, "expired", None, None, None, "ttl"); return
        if self.dry_run:
            tx = "0xDRYRUN" + intent.id
            await self._report(intent, "filled", tx, eff_price, 0.0005, "dryrun")
        else:
            raise NotImplementedError("Live execution disabled. See module docstring.")

    async def _report(self, intent, status, tx, price, slip, note):
        # crude PnL estimate: signed notional * tiny edge proxy from supporting signals
        edge = sum(s.strength * s.confidence for s in intent.supporting) / max(1, len(intent.supporting))
        sign = 1 if intent.asset_out == "ETH" else -1
        pnl = sign * edge * intent.amount_in * 0.001
        rep = ExecutionReport(intent.id, status, tx, price, slip, pnl, note)
        await self.bus.publish("exec.report", rep)
        # attribution back to strategist
        contribs = {s.agent_id: s.strength * s.confidence * sign for s in intent.supporting}
        await self.bus.publish("audit.attribution", {"pnl": pnl, "contribs": contribs})


class Auditor:
    """Append-only SQLite log + running daily PnL into a shared state dict."""
    def __init__(self, bus: Bus, db_path: Path, state: dict):
        self.bus, self.state = bus, state
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS reports(
            ts REAL, intent_id TEXT, status TEXT, tx TEXT,
            fill_price REAL, slippage REAL, pnl REAL, note TEXT)""")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS intents(
            ts REAL, id TEXT, asset_in TEXT, asset_out TEXT,
            amount_in REAL, supporting TEXT)""")
        self.conn.commit()
        bus.subscribe("intent.new", self._on_intent)
        bus.subscribe("exec.report", self._on_report)

    async def _on_intent(self, intent: TradeIntent):
        self.conn.execute("INSERT INTO intents VALUES (?,?,?,?,?,?)",
                          (time.time(), intent.id, intent.asset_in, intent.asset_out,
                           intent.amount_in,
                           json.dumps([s.__dict__ for s in intent.supporting], default=str)))
        self.conn.commit()

    async def _on_report(self, rep: ExecutionReport):
        self.conn.execute("INSERT INTO reports VALUES (?,?,?,?,?,?,?,?)",
                          (time.time(), rep.intent_id, rep.status, rep.tx_hash,
                           rep.fill_price, rep.realized_slippage, rep.pnl_estimate, rep.note))
        self.conn.commit()
        self.state["daily_pnl"] = self.state.get("daily_pnl", 0.0) + (rep.pnl_estimate or 0.0)
        log.info("REPORT %s %s pnl=%+.4f cum=%+.4f",
                 rep.intent_id, rep.status, rep.pnl_estimate or 0.0, self.state["daily_pnl"])
