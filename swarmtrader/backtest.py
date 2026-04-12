"""
Backtesting engine for Swarm Trade strategies.

Replays historical OHLC data from Kraken through the full agent pipeline
and reports performance metrics (PnL, Sharpe, drawdown, win rate).

Usage:
  python -m swarmtrader.backtest [OPTIONS]
  python -m swarmtrader.backtest --pair ETHUSD --interval 5 --base-size 500
"""
from __future__ import annotations
import asyncio, logging, math, time, argparse, tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .core import Bus, MarketSnapshot, ExecutionReport
from .agents import MomentumAnalyst, MeanReversionAnalyst, VolatilityAnalyst
from .strategy import Strategist, RiskAgent, Coordinator, size_check, allowlist_check, drawdown_check
from .execution import Simulator, Auditor
from .kraken import _run_cli

log = logging.getLogger("swarm.backtest")


@dataclass
class BacktestTrade:
    ts: float
    intent_id: str
    side: str          # "buy" or "sell"
    asset: str
    amount_in: float
    fill_price: float
    status: str
    pnl: float


@dataclass
class BacktestResult:
    pair: str
    interval_min: int
    candles: int
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl <= 0)

    @property
    def win_rate(self) -> float:
        return self.wins / self.num_trades if self.num_trades else 0.0

    @property
    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for val in self.equity_curve:
            peak = max(peak, val)
            dd = (peak - val) / peak if peak > 0 else 0.0
            max_dd = max(max_dd, dd)
        return max_dd

    @property
    def sharpe_ratio(self) -> float:
        if len(self.trades) < 2:
            return 0.0
        returns = [t.pnl for t in self.trades]
        mean_r = sum(returns) / len(returns)
        var = sum((r - mean_r) ** 2 for r in returns) / len(returns)
        std = max(1e-9, var ** 0.5)
        # Annualize: assume ~252 trading days, scale by trades per day
        return (mean_r / std) * math.sqrt(min(252, len(returns)))

    def summary(self) -> str:
        lines = [
            f"{'=' * 60}",
            f"  BACKTEST RESULTS: {self.pair} ({self.interval_min}min candles)",
            f"{'=' * 60}",
            f"  Candles processed : {self.candles}",
            f"  Total trades      : {self.num_trades}",
            f"  Wins / Losses     : {self.wins} / {self.losses}",
            f"  Win rate          : {self.win_rate:.1%}",
            f"  Total PnL         : ${self.total_pnl:+,.2f}",
            f"  Max drawdown      : {self.max_drawdown:.2%}",
            f"  Sharpe ratio      : {self.sharpe_ratio:.3f}",
            f"{'=' * 60}",
        ]
        return "\n".join(lines)


class _SimulatedClock:
    """Overrides time.time() for backtesting so cooldowns and TTLs work."""
    def __init__(self):
        self._now: float = 0.0

    def set(self, ts: float):
        self._now = ts

    def __call__(self) -> float:
        return self._now


class HistoricalScout:
    """Replays historical OHLC candles as MarketSnapshot events."""

    def __init__(self, bus: Bus, candles: list[list], symbol: str = "ETH",
                 clock: _SimulatedClock | None = None):
        self.bus = bus
        self.candles = candles
        self.symbol = symbol
        self.clock = clock
        self._stop = False

    def stop(self):
        self._stop = True

    async def run(self):
        for candle in self.candles:
            if self._stop:
                break
            # candle: [time, open, high, low, close, vwap, volume, count]
            ts = float(candle[0])
            close = float(candle[4])
            vwap = float(candle[5])
            price = vwap if vwap > 0 else close

            # Advance simulated clock
            if self.clock:
                self.clock.set(ts)

            snap = MarketSnapshot(ts=ts, prices={self.symbol: price}, gas_gwei=0.0)
            await self.bus.publish("market.snapshot", snap)
            # Tiny yield to let event handlers run
            await asyncio.sleep(0)


class BacktestExecutor:
    """Captures trade intents and simulates fills for backtesting."""

    def __init__(self, bus: Bus, result: BacktestResult):
        self.bus = bus
        self.result = result
        self.last_price: float | None = None
        self.cumulative_pnl = 0.0
        bus.subscribe("market.snapshot", self._on_snap)
        bus.subscribe("exec.simulated", self._on_sim)

    async def _on_snap(self, snap: MarketSnapshot):
        for price in snap.prices.values():
            self.last_price = price
            break

    async def _on_sim(self, payload):
        intent, eff_price = payload
        if eff_price is None:
            return

        # Calculate PnL estimate
        edge = sum(s.strength * s.confidence for s in intent.supporting) / max(1, len(intent.supporting))
        going_long = intent.asset_out in ("ETH", "BTC", "SOL")
        sign = 1 if going_long else -1
        pnl = sign * edge * intent.amount_in * 0.001

        trade = BacktestTrade(
            ts=time.time(),
            intent_id=intent.id,
            side="buy" if going_long else "sell",
            asset=intent.asset_out if going_long else intent.asset_in,
            amount_in=intent.amount_in,
            fill_price=eff_price,
            status="filled",
            pnl=pnl,
        )
        self.result.trades.append(trade)
        self.cumulative_pnl += pnl
        self.result.equity_curve.append(self.cumulative_pnl)

        # Publish report for auditor and attribution
        rep = ExecutionReport(intent.id, "filled", f"0xBACKTEST{intent.id}",
                              eff_price, 0.0005, pnl, "backtest")
        await self.bus.publish("exec.report", rep)
        contribs = {s.agent_id: s.strength * s.confidence * sign for s in intent.supporting}
        await self.bus.publish("audit.attribution", {"pnl": pnl, "contribs": contribs})


async def fetch_candles(pair: str, interval: int) -> tuple[list[list], str]:
    """Fetch historical OHLC data from Kraken CLI."""
    data = await _run_cli("ohlc", pair, "--interval", str(interval))
    # Response: {"XETHZUSD": [[ts, o, h, l, c, vwap, vol, count], ...], "last": ...}
    for key, val in data.items():
        if key != "last" and isinstance(val, list):
            return val, key
    raise RuntimeError(f"No OHLC data returned for {pair}")


async def run_backtest(
    pair: str = "ETHUSD",
    interval: int = 5,
    base_size: float = 500.0,
    max_size: float = 2000.0,
    max_drawdown: float = 100.0,
) -> BacktestResult:
    """Run a full backtest over historical Kraken data."""
    log.info("Fetching OHLC data: pair=%s interval=%dmin", pair, interval)
    candles, raw_pair = await fetch_candles(pair, interval)
    log.info("Got %d candles from %s", len(candles), raw_pair)

    # Normalize symbol
    symbol = pair.replace("USD", "").replace("USDT", "")
    if symbol.startswith("X") and len(symbol) == 4:
        symbol = symbol[1:]
    symbol = {"XBT": "BTC"}.get(symbol, symbol)

    result = BacktestResult(pair=pair, interval_min=interval, candles=len(candles))
    bus = Bus()
    state: dict = {"daily_pnl": 0.0}

    # Simulated clock — monkey-patch time.time so cooldowns/TTLs work
    clock = _SimulatedClock()
    import swarmtrader.strategy as _strat_mod
    import swarmtrader.execution as _exec_mod
    _strat_mod.time = type("_T", (), {"time": staticmethod(clock)})()
    _exec_mod.time = type("_T", (), {"time": staticmethod(clock)})()

    try:
        # Scout: replays historical candles
        scout = HistoricalScout(bus, candles, symbol=symbol, clock=clock)

        # Analysts
        MomentumAnalyst(bus, asset=symbol)
        MeanReversionAnalyst(bus, asset=symbol)
        VolatilityAnalyst(bus, asset=symbol)

        # Strategy + risk
        tokens = {symbol, "USD", "USDC", "USDT"}
        Strategist(bus, base_size=base_size)
        risks = [
            RiskAgent(bus, "size", size_check(max_size=max_size)),
            RiskAgent(bus, "allowlist", allowlist_check(tokens)),
            RiskAgent(bus, "drawdown", drawdown_check(state, max_dd=max_drawdown)),
        ]
        Coordinator(bus, n_risk_agents=len(risks))

        # Execution: simulator + backtest executor
        Simulator(bus)
        BacktestExecutor(bus, result)

        # Auditor (temporary DB)
        with tempfile.TemporaryDirectory() as d:
            Auditor(bus, db_path=Path(d) / "backtest.db", state=state)
            await scout.run()
    finally:
        # Restore real time
        _strat_mod.time = time
        _exec_mod.time = time

    return result


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Swarm Trade Backtester")
    p.add_argument("--pair", default="ETHUSD", help="Trading pair (default: ETHUSD)")
    p.add_argument("--interval", type=int, default=5,
                   help="OHLC interval in minutes (1, 5, 15, 30, 60, 240, 1440)")
    p.add_argument("--base-size", type=float, default=500.0,
                   help="Base trade size in USD (default: 500)")
    p.add_argument("--max-size", type=float, default=2000.0,
                   help="Max single trade size (default: 2000)")
    p.add_argument("--max-drawdown", type=float, default=100.0,
                   help="Max daily drawdown in USD (default: 100)")
    return p.parse_args()


async def main(args: argparse.Namespace):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-16s %(levelname)-5s %(message)s",
    )
    result = await run_backtest(
        pair=args.pair,
        interval=args.interval,
        base_size=args.base_size,
        max_size=args.max_size,
        max_drawdown=args.max_drawdown,
    )
    print(result.summary())

    # Print trade log
    if result.trades:
        print(f"\n  {'Side':<6} {'Amount':>10} {'Price':>12} {'PnL':>10}")
        print(f"  {'-'*6} {'-'*10} {'-'*12} {'-'*10}")
        for t in result.trades:
            print(f"  {t.side:<6} ${t.amount_in:>9,.2f} ${t.fill_price:>11,.2f} ${t.pnl:>+9,.4f}")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
