"""Live terminal dashboard — real-time view of agent signals, trades, and PnL."""
from __future__ import annotations
import asyncio, os, time, logging
from collections import deque
from .core import Bus, MarketSnapshot, Signal, TradeIntent, ExecutionReport

log = logging.getLogger("swarm.dash")

# ANSI colors
RST = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[91m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
MAGENTA = "\033[95m"
WHITE = "\033[97m"
BG_RED = "\033[41m"


def _color_pnl(v: float) -> str:
    if v > 0:
        return f"{GREEN}+{v:.4f}{RST}"
    elif v < 0:
        return f"{RED}{v:.4f}{RST}"
    return f"{DIM}{v:.4f}{RST}"


def _color_dir(d: str) -> str:
    if d == "long":
        return f"{GREEN}LONG {RST}"
    elif d == "short":
        return f"{RED}SHORT{RST}"
    return f"{DIM}FLAT {RST}"


class Dashboard:
    """Subscribes to all bus events and renders a live terminal dashboard."""

    def __init__(self, bus: Bus, state: dict, refresh: float = 1.0):
        self.bus = bus
        self.state = state
        self.refresh = refresh
        self._stop = False

        # State
        self.prices: dict[str, float] = {}
        self.signals: dict[str, Signal] = {}
        self.recent_trades: deque[ExecutionReport] = deque(maxlen=15)
        self.recent_intents: deque[TradeIntent] = deque(maxlen=10)
        self.trade_count = 0
        self.wins = 0
        self.losses = 0
        self.regime = "unknown"
        self.vol_damp = 1.0
        self.halted = False

        bus.subscribe("market.snapshot", self._on_snap)
        bus.subscribe("signal.momentum", self._on_signal)
        bus.subscribe("signal.mean_rev", self._on_signal)
        bus.subscribe("signal.vol", self._on_signal)
        bus.subscribe("signal.prism", self._on_signal)
        bus.subscribe("signal.orderbook", self._on_signal)
        bus.subscribe("signal.funding", self._on_signal)
        bus.subscribe("signal.spread", self._on_signal)
        bus.subscribe("signal.regime", self._on_regime)
        bus.subscribe("signal.prism_breakout", self._on_signal)
        bus.subscribe("intent.new", self._on_intent)
        bus.subscribe("exec.report", self._on_report)
        bus.subscribe("safety.halt", self._on_halt)

    def stop(self):
        self._stop = True

    async def _on_snap(self, snap: MarketSnapshot):
        self.prices.update(snap.prices)

    async def _on_signal(self, sig: Signal):
        self.signals[sig.agent_id] = sig

    async def _on_regime(self, sig: Signal):
        if "regime=" in sig.rationale:
            self.regime = sig.rationale.split("regime=")[1].split()[0]

    async def _on_intent(self, intent: TradeIntent):
        self.recent_intents.append(intent)

    async def _on_report(self, rep: ExecutionReport):
        self.recent_trades.append(rep)
        if rep.status == "filled":
            self.trade_count += 1
            if (rep.pnl_estimate or 0) > 0:
                self.wins += 1
            else:
                self.losses += 1

    async def _on_halt(self, payload: dict):
        self.halted = True

    async def run(self):
        while not self._stop:
            self._render()
            await asyncio.sleep(self.refresh)

    def _render(self):
        lines = []
        w = 80
        # Clear screen
        lines.append("\033[2J\033[H")

        # Header
        lines.append(f"{BOLD}{CYAN}{'=' * w}{RST}")
        lines.append(f"{BOLD}{CYAN}  SWARM TRADE v0.1 — Multi-Agent Autonomous Trading Platform{RST}")
        lines.append(f"{BOLD}{CYAN}{'=' * w}{RST}")

        # Status bar
        pnl = self.state.get("daily_pnl", 0.0)
        wr = f"{self.wins}/{self.wins+self.losses}" if self.wins + self.losses > 0 else "0/0"
        win_pct = (self.wins / (self.wins + self.losses) * 100) if self.wins + self.losses > 0 else 0

        halt_str = f"  {BG_RED}{WHITE} HALTED {RST}" if self.halted else f"  {GREEN}ACTIVE{RST}"
        lines.append(f"  Status:{halt_str}  Regime: {YELLOW}{self.regime:<15}{RST} "
                      f"Trades: {BOLD}{self.trade_count}{RST}  W/L: {wr} ({win_pct:.0f}%)  "
                      f"PnL: {_color_pnl(pnl)}")
        lines.append("")

        # Prices
        lines.append(f"  {BOLD}PRICES{RST}")
        price_parts = []
        for sym, price in sorted(self.prices.items()):
            price_parts.append(f"  {sym}: {WHITE}${price:,.2f}{RST}")
        lines.append("  ".join(price_parts) if price_parts else f"  {DIM}waiting for data...{RST}")
        lines.append("")

        # Signals
        lines.append(f"  {BOLD}AGENT SIGNALS{RST}")
        lines.append(f"  {'Agent':<16} {'Dir':<8} {'Strength':>10} {'Confidence':>12} {'Rationale'}")
        lines.append(f"  {'-'*16} {'-'*8} {'-'*10} {'-'*12} {'-'*30}")
        for name in ("momentum", "mean_rev", "orderbook", "funding", "prism",
                      "prism_breakout", "vol", "spread"):
            sig = self.signals.get(name)
            if sig:
                lines.append(
                    f"  {name:<16} {_color_dir(sig.direction)} "
                    f"{sig.strength:>+10.4f} {sig.confidence:>12.4f} "
                    f"{DIM}{sig.rationale[:40]}{RST}"
                )
            else:
                lines.append(f"  {name:<16} {DIM}--{RST}")
        lines.append("")

        # Recent trades
        lines.append(f"  {BOLD}RECENT TRADES{RST}")
        lines.append(f"  {'ID':<10} {'Status':<10} {'Price':>12} {'Slip':>8} {'PnL':>12} {'Note'}")
        lines.append(f"  {'-'*10} {'-'*10} {'-'*12} {'-'*8} {'-'*12} {'-'*15}")
        for rep in list(self.recent_trades)[-8:]:
            status_color = GREEN if rep.status == "filled" else RED if rep.status in ("rejected", "error") else YELLOW
            price_str = f"${rep.fill_price:,.2f}" if rep.fill_price else "--"
            slip_str = f"{rep.realized_slippage:.4f}" if rep.realized_slippage else "--"
            lines.append(
                f"  {rep.intent_id:<10} {status_color}{rep.status:<10}{RST} "
                f"{price_str:>12} {slip_str:>8} {_color_pnl(rep.pnl_estimate or 0)} "
                f"{DIM}{rep.note[:15]}{RST}"
            )
        if not self.recent_trades:
            lines.append(f"  {DIM}no trades yet...{RST}")

        lines.append("")
        lines.append(f"{BOLD}{CYAN}{'=' * w}{RST}")
        lines.append(f"  {DIM}Press Ctrl+C to stop  |  touch KILL to emergency halt{RST}")

        print("\n".join(lines), flush=True)
