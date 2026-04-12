"""Position management: trailing stops, hard stops, take-profits, break-even
locks, exposure limits, and time-based exits.

Listens to exec.report for new fills and market.snapshot for price updates.
Emits intent.new for exit orders and signal.position for strategist awareness.
"""
from __future__ import annotations
import asyncio, logging, time
from dataclasses import dataclass, field
from .core import Bus, MarketSnapshot, Signal, TradeIntent, ExecutionReport, QUOTE_ASSETS

log = logging.getLogger("swarm.positions")


# ---------------------------------------------------------------------------
# Managed position — tracks one open position with exit rules
# ---------------------------------------------------------------------------
@dataclass
class ManagedPosition:
    """A position with active stop/target management."""
    asset: str
    side: str                        # "long" or "short"
    entry_price: float
    quantity: float
    entry_ts: float = field(default_factory=time.time)

    # Hard stop-loss (maximum acceptable loss)
    hard_stop_pct: float = 0.05      # 5% hard stop — unconditional exit

    # Trailing stop (follows price, tightens on profit)
    trail_pct: float = 0.03          # 3% initial trailing stop
    highest_price: float = 0.0       # watermark for longs
    lowest_price: float = float("inf")  # watermark for shorts

    # Take profit levels (partial exits)
    tp1_pct: float = 0.02            # 2% — exit 30%
    tp2_pct: float = 0.05            # 5% — exit 30%
    tp3_pct: float = 0.10            # 10% — exit remaining
    tp1_hit: bool = False
    tp2_hit: bool = False

    # Break-even stop: after TP1, move stop to entry + small buffer
    breakeven_active: bool = False
    breakeven_buffer_pct: float = 0.003  # 0.3% above entry

    # Time-based exit
    max_hold_seconds: float = 3600.0  # 1 hour max hold

    # Remaining quantity fraction (decreases as TPs hit)
    remaining_pct: float = 1.0

    # Cost tracking
    total_cost: float = 0.0          # total USD spent entering
    total_fees: float = 0.0          # accumulated fees

    def update_extremes(self, price: float):
        if self.side == "long":
            self.highest_price = max(self.highest_price, price)
        else:
            self.lowest_price = min(self.lowest_price, price)

    def unrealized_pnl_pct(self, price: float) -> float:
        if self.side == "long":
            return (price / self.entry_price) - 1
        else:
            return (self.entry_price / price) - 1

    def unrealized_pnl_usd(self, price: float) -> float:
        pct = self.unrealized_pnl_pct(price)
        return pct * self.quantity * self.entry_price

    def hard_stop_hit(self, price: float) -> bool:
        """Unconditional stop: exit if loss exceeds hard_stop_pct."""
        pnl = self.unrealized_pnl_pct(price)
        return pnl <= -self.hard_stop_pct

    def trailing_stop_hit(self, price: float) -> bool:
        if self.side == "long":
            if self.highest_price <= 0:
                return False
            drawdown = (self.highest_price - price) / self.highest_price
            return drawdown >= self.trail_pct
        else:
            if self.lowest_price == float("inf"):
                return False
            drawup = (price - self.lowest_price) / self.lowest_price
            return drawup >= self.trail_pct

    def breakeven_stop_hit(self, price: float) -> bool:
        """After TP1, exit if price drops back to entry (+ buffer)."""
        if not self.breakeven_active:
            return False
        if self.side == "long":
            breakeven_level = self.entry_price * (1 + self.breakeven_buffer_pct)
            return price <= breakeven_level
        else:
            breakeven_level = self.entry_price * (1 - self.breakeven_buffer_pct)
            return price >= breakeven_level

    def time_expired(self) -> bool:
        return (time.time() - self.entry_ts) >= self.max_hold_seconds

    @property
    def hold_duration(self) -> float:
        return time.time() - self.entry_ts

    @property
    def notional_value(self) -> float:
        return self.quantity * self.entry_price * self.remaining_pct


# ---------------------------------------------------------------------------
# Position Manager — orchestrates all exit logic
# ---------------------------------------------------------------------------
class PositionManager:
    """Manages open positions with layered exit strategies.

    Exit priority (checked each tick):
      1. Hard stop-loss — unconditional, protects against catastrophic loss
      2. Take-profit levels — partial exits to lock in gains
      3. Break-even stop — after TP1, locks in entry price
      4. Trailing stop — follows price up, exits on drawdown
      5. Time expiry — closes stale positions

    Risk controls:
      - Max open positions (default 5)
      - Max exposure per asset (default $2000)
      - Max total exposure (default $5000)
      - Cooldown between exits to prevent order spam

    Listens to:
      - market.snapshot: checks all exit conditions
      - exec.report: tracks new fills, updates positions on partial exits

    Publishes:
      - intent.new: exit orders
      - signal.position: position-aware signals for strategist
    """

    name = "position_mgr"

    def __init__(self, bus: Bus, *,
                 trail_pct: float = 0.03,
                 hard_stop_pct: float = 0.05,
                 max_hold: float = 3600.0,
                 max_positions: int = 5,
                 max_exposure_per_asset: float = 2000.0,
                 max_total_exposure: float = 5000.0):
        self.bus = bus
        self.trail_pct = trail_pct
        self.hard_stop_pct = hard_stop_pct
        self.max_hold = max_hold
        self.max_positions = max_positions
        self.max_exposure_per_asset = max_exposure_per_asset
        self.max_total_exposure = max_total_exposure

        self.positions: dict[str, ManagedPosition] = {}
        self._cooldown: dict[str, float] = {}
        self._exit_count: int = 0
        self._exit_pnl: float = 0.0

        bus.subscribe("market.snapshot", self._on_market)
        bus.subscribe("exec.report", self._on_fill)

    # -- Public queries for risk checks and dashboard -----------------------

    @property
    def open_count(self) -> int:
        return len(self.positions)

    def exposure_for(self, asset: str) -> float:
        """Current notional exposure for an asset."""
        pos = self.positions.get(asset)
        return pos.notional_value if pos else 0.0

    def total_exposure(self) -> float:
        return sum(p.notional_value for p in self.positions.values())

    def can_open(self, asset: str, notional: float) -> tuple[bool, str]:
        """Check if a new position is allowed under exposure limits."""
        if self.open_count >= self.max_positions:
            return False, f"max_positions={self.max_positions}"
        current = self.exposure_for(asset)
        if current + notional > self.max_exposure_per_asset:
            return False, f"asset_exposure={current+notional:.0f}>{self.max_exposure_per_asset:.0f}"
        total = self.total_exposure()
        if total + notional > self.max_total_exposure:
            return False, f"total_exposure={total+notional:.0f}>{self.max_total_exposure:.0f}"
        return True, "ok"

    def summary(self) -> dict:
        return {
            "open_positions": self.open_count,
            "total_exposure": round(self.total_exposure(), 2),
            "exit_count": self._exit_count,
            "exit_pnl": round(self._exit_pnl, 4),
            "positions": {
                asset: {
                    "side": p.side,
                    "entry": round(p.entry_price, 2),
                    "qty": round(p.quantity, 6),
                    "remaining": f"{p.remaining_pct:.0%}",
                    "hold_s": round(p.hold_duration, 0),
                    "trail": f"{p.trail_pct:.1%}",
                    "tp1": p.tp1_hit, "tp2": p.tp2_hit,
                    "breakeven": p.breakeven_active,
                }
                for asset, p in self.positions.items()
            },
        }

    # -- Fill handler -------------------------------------------------------

    async def _on_fill(self, report: ExecutionReport):
        if report.status != "filled" or report.fill_price is None:
            return

        asset = report.asset
        side_str = report.side
        if not asset or not side_str:
            return

        # If this is an exit from our own managed position, update remaining
        if asset in self.positions:
            pos = self.positions[asset]
            # Exit fills reduce quantity
            is_exit = (pos.side == "long" and side_str == "sell") or \
                      (pos.side == "short" and side_str == "buy")
            if is_exit:
                pos.quantity = max(0, pos.quantity - report.quantity)
                if pos.quantity < 1e-9:
                    del self.positions[asset]
                    log.info("Position closed: %s", asset)
                return

        # New entry — create managed position
        if side_str == "buy":
            pos_side = "long"
        elif side_str == "sell":
            pos_side = "short"
        else:
            return

        # Add to existing position (scale in) or create new
        if asset in self.positions:
            existing = self.positions[asset]
            if existing.side == pos_side:
                # Scale in: update average entry and quantity
                old_cost = existing.entry_price * existing.quantity
                new_cost = report.fill_price * report.quantity
                existing.quantity += report.quantity
                existing.entry_price = (old_cost + new_cost) / existing.quantity
                existing.total_cost += new_cost
                existing.total_fees += report.fee_usd
                existing.update_extremes(report.fill_price)
                log.info("Position scaled: %s %s qty=%.6f avg=%.2f",
                         pos_side, asset, existing.quantity, existing.entry_price)
                return

        pos = ManagedPosition(
            asset=asset,
            side=pos_side,
            entry_price=report.fill_price,
            quantity=report.quantity,
            hard_stop_pct=self.hard_stop_pct,
            trail_pct=self.trail_pct,
            max_hold_seconds=self.max_hold,
            total_cost=report.fill_price * report.quantity,
            total_fees=report.fee_usd,
        )
        pos.update_extremes(report.fill_price)
        self.positions[asset] = pos
        log.info("Position opened: %s %s @ %.2f qty=%.6f",
                 pos_side, asset, report.fill_price, report.quantity)

    # -- Market tick handler ------------------------------------------------

    async def _on_market(self, snap: MarketSnapshot):
        to_close: list[tuple[str, ManagedPosition, str]] = []

        for asset, pos in list(self.positions.items()):
            price = snap.prices.get(asset)
            if price is None:
                continue

            pos.update_extremes(price)
            pnl_pct = pos.unrealized_pnl_pct(price)

            # --- 1. Hard stop-loss (highest priority) ---
            if pos.hard_stop_hit(price):
                to_close.append((asset, pos,
                    f"HARD STOP ({pnl_pct:+.2%} loss)"))
                continue

            # --- 2. Take-profit levels (partial exits) ---
            if not pos.tp1_hit and pnl_pct >= pos.tp1_pct:
                pos.tp1_hit = True
                pos.remaining_pct -= 0.30
                await self._emit_exit(asset, pos, 0.30,
                                      f"TP1 hit ({pnl_pct:+.2%})")
                # Activate break-even stop
                pos.breakeven_active = True
                # Tighten trailing stop
                pos.trail_pct = max(0.015, pos.trail_pct * 0.7)
                log.info("TP1: %s trail tightened to %.1f%%, breakeven active",
                         asset, pos.trail_pct * 100)

            if not pos.tp2_hit and pnl_pct >= pos.tp2_pct:
                pos.tp2_hit = True
                pos.remaining_pct -= 0.30
                await self._emit_exit(asset, pos, 0.30,
                                      f"TP2 hit ({pnl_pct:+.2%})")
                # Tighten trailing stop further
                pos.trail_pct = max(0.008, pos.trail_pct * 0.5)
                log.info("TP2: %s trail tightened to %.1f%%", asset, pos.trail_pct * 100)

            if pnl_pct >= pos.tp3_pct:
                to_close.append((asset, pos, f"TP3 hit ({pnl_pct:+.2%})"))
                continue

            # --- 3. Break-even stop (after TP1) ---
            if pos.breakeven_stop_hit(price):
                to_close.append((asset, pos,
                    f"break-even stop ({pnl_pct:+.2%})"))
                continue

            # --- 4. Trailing stop ---
            if pos.trailing_stop_hit(price):
                to_close.append((asset, pos,
                    f"trailing stop ({pnl_pct:+.2%})"))
                continue

            # --- 5. Time expiry ---
            if pos.time_expired():
                to_close.append((asset, pos,
                    f"time exit {pos.hold_duration:.0f}s ({pnl_pct:+.2%})"))
                continue

            # --- Emit position signal to strategist ---
            await self._emit_position_signal(asset, pos, pnl_pct)

        # Process full exits
        for asset, pos, reason in to_close:
            await self._emit_exit(asset, pos, pos.remaining_pct, reason)
            self._exit_count += 1
            del self.positions[asset]

    # -- Exit intent emission -----------------------------------------------

    async def _emit_exit(self, asset: str, pos: ManagedPosition,
                         exit_pct: float, reason: str):
        now = time.time()
        # Cooldown to prevent order spam (3s per asset)
        if now - self._cooldown.get(asset, 0) < 3.0:
            return
        self._cooldown[asset] = now

        exit_qty = pos.quantity * exit_pct
        exit_notional = exit_qty * pos.entry_price

        # Build exit intent (reverse the position)
        if pos.side == "long":
            intent = TradeIntent.new(
                asset_in=asset, asset_out="USD",
                amount_in=exit_qty,
                min_out=0, ttl=now + 30.0,
            )
        else:
            intent = TradeIntent.new(
                asset_in="USD", asset_out=asset,
                amount_in=exit_notional,
                min_out=0, ttl=now + 30.0,
            )

        log.info("EXIT %s %s %.0f%% ($%.0f) — %s",
                 pos.side, asset, exit_pct * 100, exit_notional, reason)
        await self.bus.publish("intent.new", intent)

    # -- Position-awareness signals for strategist --------------------------

    async def _emit_position_signal(self, asset: str, pos: ManagedPosition,
                                     pnl_pct: float):
        """Tell the strategist about open position state to avoid overexposure."""
        if abs(pnl_pct) < 0.005:
            return  # skip noise

        # In profit: reduce new entry appetite (don't pile on)
        # In loss: discourage adding to losers
        if pnl_pct > 0:
            strength = max(-0.5, -pnl_pct * 3)
        else:
            strength = max(-0.3, pnl_pct * 2)

        sig = Signal(
            self.name, asset, "flat",
            strength, 0.4,
            f"open_{pos.side} pnl={pnl_pct:+.2%} rem={pos.remaining_pct:.0%} "
            f"hold={pos.hold_duration:.0f}s trail={pos.trail_pct:.1%}",
        )
        await self.bus.publish("signal.position", sig)


# ---------------------------------------------------------------------------
# Risk check: max positions / exposure limits
# ---------------------------------------------------------------------------
def max_positions_check(pos_mgr: PositionManager):
    """Risk check: block new entries if position limits are hit."""
    def f(intent: TradeIntent) -> tuple[bool, str]:
        # Only check buy intents (new entries)
        if intent.asset_in.upper() in QUOTE_ASSETS:
            asset = intent.asset_out
            ok, reason = pos_mgr.can_open(asset, intent.amount_in)
            return ok, f"positions: {reason}"
        return True, f"positions: exit ok"
    return f
