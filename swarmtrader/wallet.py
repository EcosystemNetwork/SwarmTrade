"""Wallet management — cash balance, allocation limits, fund reserves, and persistence.

Tracks available capital, enforces per-asset allocation caps, reserves funds for
pending orders, and persists wallet state to SQLite so it survives restarts.
Publishes wallet events on the bus for dashboard consumption.
"""
from __future__ import annotations
import logging, sqlite3, time, json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .core import (
    Bus, TradeIntent, ExecutionReport, MarketSnapshot,
    PortfolioTracker, QUOTE_ASSETS,
)

log = logging.getLogger("swarm.wallet")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class Allocation:
    """Target allocation for a single asset."""
    asset: str
    target_pct: float = 0.0       # target % of portfolio (0-100)
    max_pct: float = 100.0        # hard cap % of portfolio
    current_pct: float = 0.0      # computed from live prices

    def drift(self) -> float:
        """How far current allocation drifts from target (signed %)."""
        return self.current_pct - self.target_pct


@dataclass
class FundReserve:
    """Funds locked for a pending order."""
    intent_id: str
    amount_usd: float
    asset: str
    side: str            # "buy" or "sell"
    reserved_at: float = field(default_factory=time.time)
    ttl: float = 30.0    # auto-release after this many seconds


@dataclass
class WalletTransaction:
    """Deposit or withdrawal record."""
    ts: float
    tx_type: Literal["deposit", "withdrawal", "fee", "pnl"]
    amount_usd: float
    note: str = ""


# ---------------------------------------------------------------------------
# WalletManager
# ---------------------------------------------------------------------------

class WalletManager:
    """Central wallet with cash tracking, allocation limits, reserves, and persistence.

    Features:
    - Cash balance: tracks available USD after buys, replenished on sells
    - Allocation limits: per-asset max % of total portfolio
    - Fund reservation: locks capital for pending orders, auto-releases on TTL
    - Rebalance signals: detects drift from target allocations
    - Persistence: saves/loads state from SQLite
    - Bus integration: publishes wallet.update, wallet.rebalance, wallet.low_funds
    """

    REBALANCE_DRIFT_THRESHOLD = 5.0  # trigger rebalance signal at 5% drift
    LOW_FUNDS_THRESHOLD = 0.10       # warn when cash < 10% of starting capital

    def __init__(
        self,
        bus: Bus,
        portfolio: PortfolioTracker,
        starting_capital: float = 10_000.0,
        db_path: Path | None = None,
        allocations: dict[str, dict] | None = None,
    ):
        self.bus = bus
        self.portfolio = portfolio
        self.starting_capital = starting_capital
        self.cash_balance = starting_capital
        self.peak_equity = starting_capital
        self.reserves: dict[str, FundReserve] = {}
        self.allocations: dict[str, Allocation] = {}
        self.transactions: list[WalletTransaction] = []
        self._db_path = db_path

        # Set up allocations
        if allocations:
            for asset, cfg in allocations.items():
                self.allocations[asset] = Allocation(
                    asset=asset,
                    target_pct=cfg.get("target_pct", 0.0),
                    max_pct=cfg.get("max_pct", 50.0),
                )

        # Persistence — load saved state if available
        if self._db_path:
            self._init_db()
            self._load_state()

        # Subscribe to bus events
        bus.subscribe("intent.new", self._on_intent)
        bus.subscribe("exec.report", self._on_report)
        bus.subscribe("market.snapshot", self._on_snapshot)

    # ── Bus Handlers ───────────────────────────────────────────────

    async def _on_intent(self, intent: TradeIntent):
        """Reserve funds when a new trade intent is created."""
        buying = intent.asset_in.upper() in QUOTE_ASSETS
        if buying:
            reserved = self.reserve(intent.id, intent.amount_in, intent.asset_out, "buy")
            if not reserved:
                log.warning("WALLET insufficient funds for intent %s: need=%.2f avail=%.2f",
                            intent.id, intent.amount_in, self.available_cash())
        else:
            # Selling — reserve the asset quantity (no cash needed)
            self.reserves[intent.id] = FundReserve(
                intent_id=intent.id,
                amount_usd=0.0,
                asset=intent.asset_in,
                side="sell",
                ttl=intent.ttl - time.time() if intent.ttl > time.time() else 30.0,
            )

    async def _on_report(self, rep: ExecutionReport):
        """Update cash balance and release reserves on execution report."""
        # Release the reserve regardless of outcome
        reserve = self.reserves.pop(rep.intent_id, None)

        if rep.status != "filled":
            # Order didn't fill — cash was reserved but not spent
            return

        if rep.side == "buy" and rep.fill_price and rep.quantity > 0:
            cost = rep.quantity * rep.fill_price + rep.fee_usd
            self.cash_balance -= cost
            self._record_tx("fee", rep.fee_usd, f"trade fee intent={rep.intent_id}")
            log.info("WALLET buy: spent=%.2f fee=%.4f cash=%.2f",
                     cost, rep.fee_usd, self.cash_balance)

        elif rep.side == "sell" and rep.fill_price and rep.quantity > 0:
            proceeds = rep.quantity * rep.fill_price - rep.fee_usd
            self.cash_balance += proceeds
            pnl = rep.pnl_estimate or 0.0
            self._record_tx("pnl", pnl, f"sell pnl intent={rep.intent_id}")
            self._record_tx("fee", rep.fee_usd, f"trade fee intent={rep.intent_id}")
            log.info("WALLET sell: proceeds=%.2f pnl=%.4f fee=%.4f cash=%.2f",
                     proceeds, pnl, rep.fee_usd, self.cash_balance)

        # Update peak equity
        equity = self.total_equity()
        if equity > self.peak_equity:
            self.peak_equity = equity

        # Check low funds
        if self.cash_balance < self.starting_capital * self.LOW_FUNDS_THRESHOLD:
            await self.bus.publish("wallet.low_funds", {
                "cash": round(self.cash_balance, 2),
                "threshold": round(self.starting_capital * self.LOW_FUNDS_THRESHOLD, 2),
            })

        # Persist and broadcast
        self._save_state()
        await self._broadcast_update()

    async def _on_snapshot(self, snap: MarketSnapshot):
        """Update allocation percentages on new prices."""
        self._expire_reserves()
        self._update_allocations()

        # Check for rebalance opportunities
        for alloc in self.allocations.values():
            if alloc.target_pct > 0 and abs(alloc.drift()) > self.REBALANCE_DRIFT_THRESHOLD:
                await self.bus.publish("wallet.rebalance", {
                    "asset": alloc.asset,
                    "target_pct": alloc.target_pct,
                    "current_pct": round(alloc.current_pct, 2),
                    "drift": round(alloc.drift(), 2),
                })

    # ── Public API ─────────────────────────────────────────────────

    def available_cash(self) -> float:
        """Cash available for new trades (total cash minus reserved)."""
        reserved = sum(r.amount_usd for r in self.reserves.values() if r.side == "buy")
        return max(0.0, self.cash_balance - reserved)

    def total_equity(self) -> float:
        """Total portfolio value: cash + positions marked to market."""
        return self.cash_balance + self.portfolio.total_equity()

    def drawdown_from_peak(self) -> float:
        """Current drawdown from peak equity (negative value)."""
        equity = self.total_equity()
        if self.peak_equity <= 0:
            return 0.0
        return (equity - self.peak_equity) / self.peak_equity * 100.0

    def reserve(self, intent_id: str, amount_usd: float, asset: str, side: str) -> bool:
        """Reserve funds for a pending order. Returns False if insufficient."""
        if side == "buy" and amount_usd > self.available_cash():
            return False
        self.reserves[intent_id] = FundReserve(
            intent_id=intent_id,
            amount_usd=amount_usd if side == "buy" else 0.0,
            asset=asset,
            side=side,
        )
        return True

    def release_reserve(self, intent_id: str) -> float:
        """Release a fund reservation. Returns the amount released."""
        reserve = self.reserves.pop(intent_id, None)
        return reserve.amount_usd if reserve else 0.0

    def deposit(self, amount_usd: float, note: str = "") -> float:
        """Add funds to wallet. Returns new cash balance."""
        self.cash_balance += amount_usd
        self.starting_capital += amount_usd
        self._record_tx("deposit", amount_usd, note or "manual deposit")
        self._save_state()
        log.info("WALLET deposit: +%.2f cash=%.2f", amount_usd, self.cash_balance)
        return self.cash_balance

    def withdraw(self, amount_usd: float, note: str = "") -> float | None:
        """Withdraw funds. Returns new balance, or None if insufficient."""
        if amount_usd > self.available_cash():
            log.warning("WALLET withdraw rejected: want=%.2f avail=%.2f",
                        amount_usd, self.available_cash())
            return None
        self.cash_balance -= amount_usd
        self._record_tx("withdrawal", amount_usd, note or "manual withdrawal")
        self._save_state()
        log.info("WALLET withdraw: -%.2f cash=%.2f", amount_usd, self.cash_balance)
        return self.cash_balance

    def can_afford(self, amount_usd: float) -> bool:
        """Check if wallet has enough available cash for a trade."""
        return amount_usd <= self.available_cash()

    def check_allocation(self, asset: str, trade_usd: float) -> tuple[bool, str]:
        """Check if a trade would violate allocation limits.
        Returns (ok, reason)."""
        alloc = self.allocations.get(asset)
        if not alloc:
            return True, "no allocation limit set"

        equity = self.total_equity()
        if equity <= 0:
            return False, "zero equity"

        # Current position value
        pos = self.portfolio.get(asset)
        price = self.portfolio.last_prices.get(asset, pos.avg_entry)
        current_value = pos.quantity * price
        new_value = current_value + trade_usd
        new_pct = (new_value / equity) * 100.0

        if new_pct > alloc.max_pct:
            return False, (f"allocation breach: {asset} would be {new_pct:.1f}% "
                          f"(max={alloc.max_pct:.1f}%)")
        return True, f"allocation ok: {new_pct:.1f}% <= {alloc.max_pct:.1f}%"

    def set_allocation(self, asset: str, target_pct: float = 0.0,
                       max_pct: float = 50.0):
        """Set or update allocation target and limit for an asset."""
        if asset in self.allocations:
            self.allocations[asset].target_pct = target_pct
            self.allocations[asset].max_pct = max_pct
        else:
            self.allocations[asset] = Allocation(
                asset=asset, target_pct=target_pct, max_pct=max_pct,
            )
        self._save_state()

    def summary(self) -> dict:
        """Full wallet summary for API/dashboard."""
        equity = self.total_equity()
        return {
            "cash_balance": round(self.cash_balance, 2),
            "available_cash": round(self.available_cash(), 2),
            "reserved_funds": round(
                sum(r.amount_usd for r in self.reserves.values()), 2
            ),
            "total_equity": round(equity, 2),
            "starting_capital": round(self.starting_capital, 2),
            "peak_equity": round(self.peak_equity, 2),
            "drawdown_pct": round(self.drawdown_from_peak(), 2),
            "return_pct": round(
                ((equity - self.starting_capital) / self.starting_capital * 100.0)
                if self.starting_capital > 0 else 0.0, 2
            ),
            "portfolio": self.portfolio.summary(),
            "allocations": {
                a: {
                    "target_pct": alloc.target_pct,
                    "max_pct": alloc.max_pct,
                    "current_pct": round(alloc.current_pct, 2),
                    "drift": round(alloc.drift(), 2),
                }
                for a, alloc in self.allocations.items()
            },
            "pending_reserves": len(self.reserves),
            "recent_transactions": [
                {"ts": tx.ts, "type": tx.tx_type, "amount": round(tx.amount_usd, 4),
                 "note": tx.note}
                for tx in self.transactions[-20:]
            ],
        }

    # ── Internal ───────────────────────────────────────────────────

    def _update_allocations(self):
        """Recompute current allocation percentages."""
        equity = self.total_equity()
        if equity <= 0:
            return
        for asset, alloc in self.allocations.items():
            pos = self.portfolio.get(asset)
            price = self.portfolio.last_prices.get(asset, pos.avg_entry)
            value = pos.quantity * price
            alloc.current_pct = (value / equity) * 100.0

    def _expire_reserves(self):
        """Release reserves that have exceeded their TTL."""
        now = time.time()
        expired = [
            rid for rid, r in self.reserves.items()
            if now - r.reserved_at > r.ttl
        ]
        for rid in expired:
            released = self.reserves.pop(rid)
            log.info("WALLET reserve expired: intent=%s amount=%.2f",
                     rid, released.amount_usd)

    def _record_tx(self, tx_type: str, amount: float, note: str = ""):
        self.transactions.append(WalletTransaction(
            ts=time.time(), tx_type=tx_type, amount_usd=amount, note=note,
        ))
        # Keep last 1000 transactions in memory
        if len(self.transactions) > 1000:
            self.transactions = self.transactions[-1000:]

    async def _broadcast_update(self):
        await self.bus.publish("wallet.update", self.summary())

    # ── Persistence ────────────────────────────────────────────────

    def _init_db(self):
        if not self._db_path:
            return
        conn = sqlite3.connect(self._db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS wallet_state(
            key TEXT PRIMARY KEY, value TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS wallet_transactions(
            ts REAL, tx_type TEXT, amount REAL, note TEXT)""")
        conn.commit()
        conn.close()

    def _save_state(self):
        if not self._db_path:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            state = json.dumps({
                "cash_balance": self.cash_balance,
                "starting_capital": self.starting_capital,
                "peak_equity": self.peak_equity,
                "allocations": {
                    a: {"target_pct": al.target_pct, "max_pct": al.max_pct}
                    for a, al in self.allocations.items()
                },
            })
            conn.execute(
                "INSERT OR REPLACE INTO wallet_state(key, value) VALUES (?, ?)",
                ("state", state),
            )
            # Persist recent transactions
            for tx in self.transactions[-10:]:
                conn.execute(
                    "INSERT OR IGNORE INTO wallet_transactions VALUES (?, ?, ?, ?)",
                    (tx.ts, tx.tx_type, tx.amount_usd, tx.note),
                )
            conn.commit()
            conn.close()
        except Exception as e:
            log.warning("WALLET save failed: %s", e)

    def _load_state(self):
        if not self._db_path:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            row = conn.execute(
                "SELECT value FROM wallet_state WHERE key='state'"
            ).fetchone()
            if row:
                saved = json.loads(row[0])
                self.cash_balance = saved.get("cash_balance", self.starting_capital)
                self.starting_capital = saved.get("starting_capital", self.starting_capital)
                self.peak_equity = saved.get("peak_equity", self.peak_equity)
                for asset, cfg in saved.get("allocations", {}).items():
                    self.allocations[asset] = Allocation(
                        asset=asset,
                        target_pct=cfg.get("target_pct", 0.0),
                        max_pct=cfg.get("max_pct", 50.0),
                    )
                log.info("WALLET loaded: cash=%.2f capital=%.2f peak=%.2f",
                         self.cash_balance, self.starting_capital, self.peak_equity)

            # Load transaction history
            rows = conn.execute(
                "SELECT ts, tx_type, amount, note FROM wallet_transactions "
                "ORDER BY ts DESC LIMIT 100"
            ).fetchall()
            self.transactions = [
                WalletTransaction(ts=r[0], tx_type=r[1], amount_usd=r[2], note=r[3])
                for r in reversed(rows)
            ]
            conn.close()
        except Exception as e:
            log.warning("WALLET load failed (using defaults): %s", e)


# ---------------------------------------------------------------------------
# Risk check functions for integration with RiskAgent
# ---------------------------------------------------------------------------

def funds_check(wallet: WalletManager):
    """Risk check: reject trades that exceed available cash."""
    def f(intent: TradeIntent) -> tuple[bool, str]:
        buying = intent.asset_in.upper() in QUOTE_ASSETS
        if not buying:
            return True, "sell — no cash needed"
        avail = wallet.available_cash()
        ok = intent.amount_in <= avail
        return ok, f"need={intent.amount_in:.0f} avail={avail:.0f}"
    return f


def allocation_check(wallet: WalletManager):
    """Risk check: reject trades that breach allocation limits."""
    def f(intent: TradeIntent) -> tuple[bool, str]:
        buying = intent.asset_in.upper() in QUOTE_ASSETS
        if not buying:
            return True, "sell — no allocation concern"
        asset = intent.asset_out
        return wallet.check_allocation(asset, intent.amount_in)
    return f
