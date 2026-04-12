"""Simulator, executor (dry-run safe), and SQLite auditor.

PnL is calculated from actual price deltas via PortfolioTracker, not signal
edge proxies. Every fill updates a real position ledger with cost basis,
and sells compute realized PnL = (fill - avg_entry) * qty - fees.
"""
from __future__ import annotations
import logging, sqlite3, time, json
from pathlib import Path
from .core import (
    Bus, TradeIntent, ExecutionReport, MarketSnapshot,
    PortfolioTracker, QUOTE_ASSETS,
)
from .safety import KillSwitch

log = logging.getLogger("swarm")


def _is_buy(intent: TradeIntent) -> bool:
    """True if the intent is buying a base asset with a quote asset."""
    return intent.asset_in.upper() in QUOTE_ASSETS


def _base_asset(intent: TradeIntent) -> str:
    """Extract the base (non-quote) asset from an intent."""
    if intent.asset_in.upper() in QUOTE_ASSETS:
        return intent.asset_out
    return intent.asset_in


class Simulator:
    """Quotes against the latest mid price with size-dependent slippage.
    Slippage model: 5 bps per $1k notional (linear), applied adversely."""
    SLIPPAGE_BPS_PER_1K = 5

    def __init__(self, bus: Bus):
        self.bus = bus
        self.last_price: float | None = None
        bus.subscribe("market.snapshot", self._on_snap)
        bus.subscribe("exec.go", self._on_go)

    async def _on_snap(self, snap: MarketSnapshot):
        for price in snap.prices.values():
            self.last_price = price
            break

    async def _on_go(self, intent: TradeIntent):
        if self.last_price is None:
            await self.bus.publish("exec.simulated", (intent, None))
            return
        bps = (intent.amount_in / 1000.0) * self.SLIPPAGE_BPS_PER_1K
        slip = bps / 10_000.0
        buying = _is_buy(intent)
        if buying:
            eff = self.last_price * (1 + slip)
            out = intent.amount_in / eff
        else:
            eff = self.last_price * (1 - slip)
            out = intent.amount_in * eff
        intent.min_out = out * 0.995
        await self.bus.publish("exec.simulated", (intent, eff))


class Executor:
    """Dry-run executor with real position tracking via PortfolioTracker.
    PnL comes from actual price deltas, not signal proxies."""

    def __init__(self, bus: Bus, kill_switch: "KillSwitch", dry_run: bool = True,
                 portfolio: PortfolioTracker | None = None):
        self.bus = bus
        self.kill_switch = kill_switch
        self.dry_run = dry_run
        self.portfolio = portfolio or PortfolioTracker()
        bus.subscribe("exec.simulated", self._on_sim)
        bus.subscribe("market.snapshot", self._on_snap)

    async def _on_snap(self, snap: MarketSnapshot):
        self.portfolio.update_prices(snap.prices)

    async def _on_sim(self, payload):
        intent, eff_price = payload
        if self.kill_switch.active:
            await self._report(intent, "rejected", None, None, 0.0, 0.0, 0.0, "kill_switch")
            return
        if eff_price is None:
            await self._report(intent, "rejected", None, None, 0.0, 0.0, 0.0, "no_quote")
            return
        if time.time() > intent.ttl:
            await self._report(intent, "expired", None, None, 0.0, 0.0, 0.0, "ttl")
            return
        if self.dry_run:
            buying = _is_buy(intent)
            asset = _base_asset(intent)

            if buying:
                quantity = intent.amount_in / eff_price
                fee, pnl = self.portfolio.buy(asset, quantity, eff_price)
                side = "buy"
            else:
                pos = self.portfolio.get(asset)
                quantity = min(intent.amount_in, pos.quantity)
                if quantity < 1e-9:
                    await self._report(intent, "rejected", None, None, 0.0, 0.0, 0.0, "no_position")
                    return
                fee, pnl = self.portfolio.sell(asset, quantity, eff_price)
                side = "sell"

            tx = "0xDRYRUN" + intent.id
            mid = self.portfolio.last_prices.get(asset, eff_price)
            slippage = abs(eff_price - mid) / max(1e-9, eff_price)
            await self._report(intent, "filled", tx, eff_price, slippage, pnl, fee,
                               f"dryrun {side} {quantity:.6f} {asset}",
                               side, quantity, asset)
        else:
            raise NotImplementedError("Live execution disabled.")

    async def _report(self, intent, status, tx, price, slip, pnl, fee, note,
                      side="", quantity=0.0, asset=""):
        rep = ExecutionReport(
            intent.id, status, tx, price, slip, pnl, note,
            side=side, quantity=quantity, asset=asset or _base_asset(intent),
            fee_usd=fee,
        )
        await self.bus.publish("exec.report", rep)
        if status == "filled" and intent.supporting:
            sign = 1 if _is_buy(intent) else -1
            contribs = {s.agent_id: s.strength * s.confidence * sign
                        for s in intent.supporting}
            await self.bus.publish("audit.attribution", {"pnl": pnl, "contribs": contribs})


class Auditor:
    """Append-only SQLite log + running daily PnL from real fills."""

    def __init__(self, bus: Bus, db_path: Path, state: dict,
                 portfolio: PortfolioTracker | None = None):
        self.bus, self.state = bus, state
        self.portfolio = portfolio
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""CREATE TABLE IF NOT EXISTS reports(
            ts REAL, intent_id TEXT, status TEXT, tx TEXT,
            fill_price REAL, slippage REAL, pnl REAL, fee REAL,
            side TEXT, quantity REAL, asset TEXT, note TEXT)""")
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
        # Idempotency: skip duplicate reports for the same intent+status
        dedup_key = f"audit:{rep.intent_id}:{rep.status}"
        if self.bus.is_duplicate(dedup_key):
            log.debug("Skipping duplicate report: %s", dedup_key)
            return
        self.conn.execute(
            "INSERT INTO reports VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), rep.intent_id, rep.status, rep.tx_hash,
             rep.fill_price, rep.realized_slippage, rep.pnl_estimate,
             rep.fee_usd, rep.side, rep.quantity, rep.asset, rep.note))
        self.conn.commit()
        pnl = rep.pnl_estimate or 0.0
        self.state["daily_pnl"] = self.state.get("daily_pnl", 0.0) + pnl
        if rep.status == "filled":
            self.state["trade_count"] = self.state.get("trade_count", 0) + 1
            self.state["total_fees"] = self.state.get("total_fees", 0.0) + rep.fee_usd
        portfolio_str = ""
        if self.portfolio:
            portfolio_str = f" equity={self.portfolio.total_equity():+.2f} fees={self.portfolio.total_fees():.4f}"
        log.info("REPORT %s %s %s qty=%.6f pnl=%+.4f fee=%.4f cum=%+.4f%s",
                 rep.intent_id, rep.status, rep.side, rep.quantity,
                 pnl, rep.fee_usd, self.state["daily_pnl"], portfolio_str)
