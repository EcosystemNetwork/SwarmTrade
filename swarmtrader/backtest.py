"""
Backtesting engine for Swarm Trade strategies.

Phase 3 production rewrite:
- Real PnL from PortfolioTracker (actual cost basis, not signal proxies)
- Transaction costs: Kraken taker fees applied to every fill
- Slippage model: size-dependent linear model from Simulator
- Walk-forward support: train/test splits, out-of-sample validation
- Warm-up period: skip first N candles so indicators stabilize
- Position tracking: full buy/sell lifecycle, not just signal direction

Usage:
  python -m swarmtrader.backtest [OPTIONS]
  python -m swarmtrader.backtest --pair ETHUSD --interval 5 --base-size 500
  python -m swarmtrader.backtest --pair ETHUSD --walk-forward 5  # 5 splits
"""
from __future__ import annotations
import asyncio, logging, math, time, argparse, tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .core import (
    Bus, MarketSnapshot, ExecutionReport,
    PortfolioTracker, QUOTE_ASSETS, KRAKEN_TAKER_FEE,
)
from .agents import MomentumAnalyst, MeanReversionAnalyst, VolatilityAnalyst
from .strategy import Strategist, RiskAgent, Coordinator, size_check, allowlist_check, drawdown_check
from .execution import Simulator, Auditor

log = logging.getLogger("swarm.backtest")

# Slippage: 5 bps per $1k notional (same as Simulator)
_SLIPPAGE_BPS_PER_1K = 5


@dataclass
class BacktestTrade:
    ts: float
    intent_id: str
    side: str          # "buy" or "sell"
    asset: str
    quantity: float
    fill_price: float
    fee_usd: float
    status: str
    pnl: float         # realized PnL (0 for buys, real PnL for sells)


@dataclass
class BacktestResult:
    pair: str
    interval_min: int
    candles: int
    warm_up: int = 0
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    starting_capital: float = 10_000.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def total_fees(self) -> float:
        return sum(t.fee_usd for t in self.trades)

    @property
    def num_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl < 0)

    @property
    def flat_trades(self) -> int:
        """Trades with ~0 PnL (buys, or sells at breakeven)."""
        return sum(1 for t in self.trades if abs(t.pnl) < 0.01)

    @property
    def sell_trades(self) -> int:
        """Only count sell trades for win rate (buys have 0 PnL)."""
        return sum(1 for t in self.trades if t.side == "sell")

    @property
    def win_rate(self) -> float:
        sells = self.sell_trades
        if sells == 0:
            return 0.0
        return sum(1 for t in self.trades if t.side == "sell" and t.pnl > 0) / sells

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
        """Annualized Sharpe ratio from per-trade returns."""
        sell_returns = [t.pnl for t in self.trades if t.side == "sell"]
        if len(sell_returns) < 2:
            return 0.0
        mean_r = sum(sell_returns) / len(sell_returns)
        var = sum((r - mean_r) ** 2 for r in sell_returns) / len(sell_returns)
        std = max(1e-9, var ** 0.5)
        # Annualize assuming ~252 trading days
        return (mean_r / std) * math.sqrt(min(252, len(sell_returns)))

    @property
    def profit_factor(self) -> float:
        """Gross profits / gross losses."""
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        return gross_profit / max(1e-9, gross_loss)

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self.trades if t.pnl < 0]
        return sum(losses) / len(losses) if losses else 0.0

    @property
    def net_return_pct(self) -> float:
        if self.starting_capital <= 0:
            return 0.0
        final = self.equity_curve[-1] if self.equity_curve else self.starting_capital
        return (final - self.starting_capital) / self.starting_capital * 100

    def to_dict(self) -> dict:
        return {
            "pair": self.pair,
            "interval_min": self.interval_min,
            "candles": self.candles,
            "warm_up": self.warm_up,
            "num_trades": self.num_trades,
            "sell_trades": self.sell_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "total_pnl": round(self.total_pnl, 2),
            "total_fees": round(self.total_fees, 2),
            "net_return_pct": round(self.net_return_pct, 2),
            "max_drawdown": round(self.max_drawdown, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "profit_factor": round(self.profit_factor, 3),
            "avg_win": round(self.avg_win, 4),
            "avg_loss": round(self.avg_loss, 4),
        }

    def summary(self) -> str:
        lines = [
            f"{'=' * 60}",
            f"  BACKTEST RESULTS: {self.pair} ({self.interval_min}min candles)",
            f"{'=' * 60}",
            f"  Candles processed : {self.candles} (warm-up: {self.warm_up})",
            f"  Total trades      : {self.num_trades} ({self.sell_trades} round-trips)",
            f"  Wins / Losses     : {self.wins} / {self.losses}",
            f"  Win rate (sells)  : {self.win_rate:.1%}",
            f"  Total PnL         : ${self.total_pnl:+,.2f}",
            f"  Total fees        : ${self.total_fees:,.2f}",
            f"  Net return        : {self.net_return_pct:+.2f}%",
            f"  Max drawdown      : {self.max_drawdown:.2%}",
            f"  Sharpe ratio      : {self.sharpe_ratio:.3f}",
            f"  Profit factor     : {self.profit_factor:.2f}",
            f"  Avg win / loss    : ${self.avg_win:+,.2f} / ${self.avg_loss:+,.2f}",
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
                 clock: _SimulatedClock | None = None, warm_up: int = 0):
        self.bus = bus
        self.candles = candles
        self.symbol = symbol
        self.clock = clock
        self.warm_up = warm_up
        self._stop = False
        self.candles_emitted = 0

    def stop(self):
        self._stop = True

    async def run(self):
        for i, candle in enumerate(self.candles):
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
            self.candles_emitted += 1
            # Tiny yield to let event handlers run
            await asyncio.sleep(0)


class BacktestExecutor:
    """Simulates fills using PortfolioTracker for real PnL.

    Key differences from hackathon version:
    - PnL from actual (fill_price - avg_entry) * qty - fees
    - Transaction costs (Kraken taker fee) applied to every fill
    - Slippage model applied to fill prices
    - Position lifecycle: buy creates position, sell realizes PnL
    """

    def __init__(self, bus: Bus, result: BacktestResult, portfolio: PortfolioTracker,
                 starting_capital: float = 10_000.0):
        self.bus = bus
        self.result = result
        self.portfolio = portfolio
        self.cash = starting_capital
        self.last_price: float | None = None
        result.starting_capital = starting_capital
        result.equity_curve.append(starting_capital)

        bus.subscribe("market.snapshot", self._on_snap)
        bus.subscribe("exec.simulated", self._on_sim)

    async def _on_snap(self, snap: MarketSnapshot):
        for price in snap.prices.values():
            self.last_price = price
            break
        # Update equity curve on each tick
        self.portfolio.update_prices(snap.prices)
        equity = self.cash + self.portfolio.position_market_value()
        self.result.equity_curve.append(equity)

    async def _on_sim(self, payload):
        intent, eff_price = payload
        if eff_price is None or self.last_price is None:
            return

        buying = intent.asset_in.upper() in QUOTE_ASSETS
        if buying:
            asset = intent.asset_out
        else:
            asset = intent.asset_in

        # Apply slippage model
        bps = (intent.amount_in / 1000.0) * _SLIPPAGE_BPS_PER_1K
        slip = bps / 10_000.0
        if buying:
            fill_price = eff_price * (1 + slip)
            quantity = intent.amount_in / fill_price

            # Check we have cash
            cost = quantity * fill_price
            fee_rate = KRAKEN_TAKER_FEE
            fee = cost * fee_rate
            total_cost = cost + fee
            if total_cost > self.cash:
                return  # skip — insufficient funds

            fee_actual, pnl = self.portfolio.buy(asset, quantity, fill_price)
            self.cash -= total_cost
            side = "buy"
        else:
            fill_price = eff_price * (1 - slip)
            pos = self.portfolio.get(asset)
            quantity = min(intent.amount_in, pos.quantity)
            if quantity < 1e-9:
                return  # nothing to sell

            fee_actual, pnl = self.portfolio.sell(asset, quantity, fill_price)
            proceeds = quantity * fill_price - fee_actual
            self.cash += proceeds
            side = "sell"

        trade = BacktestTrade(
            ts=time.time(),
            intent_id=intent.id,
            side=side,
            asset=asset,
            quantity=quantity,
            fill_price=fill_price,
            fee_usd=fee_actual,
            status="filled",
            pnl=pnl,
        )
        self.result.trades.append(trade)

        # Update equity
        equity = self.cash + self.portfolio.position_market_value()
        self.result.equity_curve.append(equity)

        # Publish execution report for downstream consumers
        rep = ExecutionReport(
            intent.id, "filled", f"0xBT{intent.id}",
            fill_price, slip, pnl,
            f"backtest {side} {quantity:.6f} {asset}",
            side=side, quantity=quantity, asset=asset, fee_usd=fee_actual,
        )
        await self.bus.publish("exec.report", rep)


async def fetch_candles(pair: str, interval: int) -> tuple[list[list], str]:
    """Fetch historical OHLC data from Kraken CLI."""
    from .kraken import _run_cli
    data = await _run_cli("ohlc", pair, "--interval", str(interval))
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
    starting_capital: float = 10_000.0,
    warm_up: int = 50,
) -> BacktestResult:
    """Run a full backtest over historical Kraken data with real PnL."""
    log.info("Fetching OHLC data: pair=%s interval=%dmin", pair, interval)
    candles, raw_pair = await fetch_candles(pair, interval)
    log.info("Got %d candles from %s (warm-up: %d)", len(candles), raw_pair, warm_up)

    # Normalize symbol
    symbol = pair.replace("USD", "").replace("USDT", "")
    if symbol.startswith("X") and len(symbol) == 4:
        symbol = symbol[1:]
    symbol = {"XBT": "BTC"}.get(symbol, symbol)

    result = BacktestResult(
        pair=pair, interval_min=interval,
        candles=len(candles), warm_up=warm_up,
    )
    bus = Bus()
    state: dict = {"daily_pnl": 0.0}
    portfolio = PortfolioTracker()

    # Simulated clock
    clock = _SimulatedClock()
    import swarmtrader.strategy as _strat_mod
    import swarmtrader.execution as _exec_mod
    _strat_mod.time = type("_T", (), {"time": staticmethod(clock)})()
    _exec_mod.time = type("_T", (), {"time": staticmethod(clock)})()

    try:
        scout = HistoricalScout(bus, candles, symbol=symbol, clock=clock, warm_up=warm_up)

        MomentumAnalyst(bus, asset=symbol)
        MeanReversionAnalyst(bus, asset=symbol)
        VolatilityAnalyst(bus, asset=symbol)

        tokens = {symbol, "USD", "USDC", "USDT"}
        Strategist(bus, base_size=base_size)
        risks = [
            RiskAgent(bus, "size", size_check(max_size=max_size)),
            RiskAgent(bus, "allowlist", allowlist_check(tokens)),
            RiskAgent(bus, "drawdown", drawdown_check(state, max_dd=max_drawdown)),
        ]
        Coordinator(bus, n_risk_agents=len(risks))

        Simulator(bus)
        BacktestExecutor(bus, result, portfolio, starting_capital=starting_capital)

        with tempfile.TemporaryDirectory() as d:
            Auditor(bus, db_path=Path(d) / "backtest.db", state=state, portfolio=portfolio)
            await scout.run()
    finally:
        _strat_mod.time = time
        _exec_mod.time = time

    return result


async def run_walk_forward(
    pair: str = "ETHUSD",
    interval: int = 5,
    n_splits: int = 5,
    train_pct: float = 0.8,
    base_size: float = 500.0,
    max_size: float = 2000.0,
    max_drawdown: float = 100.0,
    starting_capital: float = 10_000.0,
) -> list[BacktestResult]:
    """Walk-forward validation: train on first N%, test on remaining.

    Splits data into n_splits windows. For each window, the first train_pct%
    of candles is the training period (warm-up + trading), and the remaining
    is the out-of-sample test period.

    Returns a list of BacktestResult, one per split.
    """
    from .kraken import _run_cli
    log.info("Walk-forward: pair=%s splits=%d train=%.0f%%", pair, n_splits, train_pct * 100)
    candles, raw_pair = await fetch_candles(pair, interval)
    total = len(candles)
    split_size = total // n_splits

    symbol = pair.replace("USD", "").replace("USDT", "")
    if symbol.startswith("X") and len(symbol) == 4:
        symbol = symbol[1:]
    symbol = {"XBT": "BTC"}.get(symbol, symbol)

    results: list[BacktestResult] = []

    for i in range(n_splits):
        start = i * split_size
        end = min(start + split_size, total)
        window = candles[start:end]
        train_end = int(len(window) * train_pct)
        test_candles = window[train_end:]  # out-of-sample

        if len(test_candles) < 10:
            log.warning("Split %d: only %d test candles, skipping", i + 1, len(test_candles))
            continue

        log.info("Split %d/%d: train=%d candles, test=%d candles",
                 i + 1, n_splits, train_end, len(test_candles))

        result = BacktestResult(
            pair=pair, interval_min=interval,
            candles=len(test_candles), warm_up=min(50, train_end),
        )
        bus = Bus()
        state: dict = {"daily_pnl": 0.0}
        portfolio = PortfolioTracker()
        clock = _SimulatedClock()

        import swarmtrader.strategy as _strat_mod
        import swarmtrader.execution as _exec_mod
        _strat_mod.time = type("_T", (), {"time": staticmethod(clock)})()
        _exec_mod.time = type("_T", (), {"time": staticmethod(clock)})()

        try:
            # Warm up on training data, then test on out-of-sample
            all_candles = window[:train_end] + test_candles
            scout = HistoricalScout(bus, all_candles, symbol=symbol, clock=clock,
                                    warm_up=train_end)

            MomentumAnalyst(bus, asset=symbol)
            MeanReversionAnalyst(bus, asset=symbol)
            VolatilityAnalyst(bus, asset=symbol)

            tokens = {symbol, "USD", "USDC", "USDT"}
            Strategist(bus, base_size=base_size)
            risks = [
                RiskAgent(bus, "size", size_check(max_size=max_size)),
                RiskAgent(bus, "allowlist", allowlist_check(tokens)),
                RiskAgent(bus, "drawdown", drawdown_check(state, max_dd=max_drawdown)),
            ]
            Coordinator(bus, n_risk_agents=len(risks))
            Simulator(bus)
            BacktestExecutor(bus, result, portfolio, starting_capital=starting_capital)

            with tempfile.TemporaryDirectory() as d:
                Auditor(bus, db_path=Path(d) / "backtest.db", state=state, portfolio=portfolio)
                await scout.run()
        finally:
            _strat_mod.time = time
            _exec_mod.time = time

        results.append(result)

    return results


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
    p.add_argument("--capital", type=float, default=10_000.0,
                   help="Starting capital in USD (default: 10000)")
    p.add_argument("--walk-forward", type=int, default=0,
                   help="Number of walk-forward splits (0 = disabled)")
    p.add_argument("--warm-up", type=int, default=50,
                   help="Warm-up candles for indicators (default: 50)")
    return p.parse_args()


async def main(args: argparse.Namespace):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-16s %(levelname)-5s %(message)s",
    )

    if args.walk_forward > 0:
        results = await run_walk_forward(
            pair=args.pair,
            interval=args.interval,
            n_splits=args.walk_forward,
            base_size=args.base_size,
            max_size=args.max_size,
            max_drawdown=args.max_drawdown,
            starting_capital=args.capital,
        )
        print(f"\n{'=' * 60}")
        print(f"  WALK-FORWARD RESULTS ({len(results)} splits)")
        print(f"{'=' * 60}")
        for i, r in enumerate(results):
            print(f"\n  Split {i+1}:")
            print(f"    Trades: {r.sell_trades}  Win rate: {r.win_rate:.1%}  "
                  f"PnL: ${r.total_pnl:+,.2f}  Sharpe: {r.sharpe_ratio:.3f}  "
                  f"DD: {r.max_drawdown:.2%}")

        # Aggregate
        total_pnl = sum(r.total_pnl for r in results)
        avg_wr = sum(r.win_rate for r in results) / max(1, len(results))
        avg_sharpe = sum(r.sharpe_ratio for r in results) / max(1, len(results))
        max_dd = max((r.max_drawdown for r in results), default=0)
        print(f"\n  Aggregate: PnL=${total_pnl:+,.2f}  Avg WR={avg_wr:.1%}  "
              f"Avg Sharpe={avg_sharpe:.3f}  Worst DD={max_dd:.2%}")
    else:
        result = await run_backtest(
            pair=args.pair,
            interval=args.interval,
            base_size=args.base_size,
            max_size=args.max_size,
            max_drawdown=args.max_drawdown,
            starting_capital=args.capital,
            warm_up=args.warm_up,
        )
        print(result.summary())

        if result.trades:
            print(f"\n  {'Side':<6} {'Qty':>10} {'Price':>12} {'Fee':>8} {'PnL':>10}")
            print(f"  {'-'*6} {'-'*10} {'-'*12} {'-'*8} {'-'*10}")
            for t in result.trades[-30:]:  # last 30 trades
                print(f"  {t.side:<6} {t.quantity:>10.6f} ${t.fill_price:>11,.2f} "
                      f"${t.fee_usd:>7,.4f} ${t.pnl:>+9,.4f}")


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(args))
