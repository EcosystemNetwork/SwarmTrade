"""
Swarm Trade — Multi-agent autonomous trading platform.

Modes:
  mock    — simulated prices, dry-run executor (default, no keys needed)
  paper   — real Kraken prices, paper trading via Kraken CLI
  live    — real Kraken prices, real order execution (requires API keys)

Run:
  python -m swarmtrader.main [mock|paper|live] [seconds]
  python -m swarmtrader.main paper 300 --pairs ETHUSD BTCUSD SOLUSD
  python -m swarmtrader.main paper 600 --pairs ETHUSD --ws --dashboard

Environment variables:
  KRAKEN_API_KEY      — Kraken API key (required for live mode)
  KRAKEN_PRIVATE_KEY  — Kraken private key (required for live mode)
  PRISM_API_KEY       — PRISM/Strykr API key (optional, enables AI signals)
"""
from __future__ import annotations
import argparse, asyncio, logging, os
from pathlib import Path

from dotenv import load_dotenv

from . import (
    Bus,
    # Data scouts
    MockScout, KrakenScout, KrakenWSScout,
    # Analysts
    MomentumAnalyst, MeanReversionAnalyst, VolatilityAnalyst,
    # Advanced agents
    OrderBookAgent, FundingRateAgent, SpreadAgent, RegimeAgent,
    # Strategy + risk
    Strategist, RiskAgent, Coordinator,
    size_check, allowlist_check, drawdown_check,
    # Execution
    Simulator, Executor, KrakenExecutor, Auditor,
    # Safety
    CircuitBreaker, PositionFlattener,
    # External signals
    PRISMSignalAgent,
    # Dashboard
    Dashboard,
)


# Mapping of simple asset names to Kraken pair + futures symbol
ASSET_CONFIG = {
    "ETH": {"pair": "ETHUSD", "futures": "PF_ETHUSD"},
    "BTC": {"pair": "XBTUSD", "futures": "PF_XBTUSD"},
    "SOL": {"pair": "SOLUSD", "futures": "PF_SOLUSD"},
    "XRP": {"pair": "XRPUSD", "futures": "PF_XRPUSD"},
    "ADA": {"pair": "ADAUSD", "futures": "PF_ADAUSD"},
    "DOT": {"pair": "DOTUSD", "futures": "PF_DOTUSD"},
    "LINK": {"pair": "LINKUSD", "futures": "PF_LINKUSD"},
    "AVAX": {"pair": "AVAXUSD", "futures": "PF_AVAXUSD"},
}


def _pairs_to_assets(pairs: list[str]) -> list[str]:
    """Extract base asset symbols from Kraken pair names."""
    assets = []
    for pair in pairs:
        p = pair.upper()
        for quote in ("ZUSD", "USD", "USDT"):
            if p.endswith(quote):
                base = p[:-len(quote)]
                break
        else:
            base = p
        if base.startswith("X") and len(base) == 4:
            base = base[1:]
        base = {"XBT": "BTC"}.get(base, base)
        assets.append(base)
    return assets


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Swarm Trade — AI trading agent swarm",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("mode", nargs="?", default="mock",
                   choices=["mock", "paper", "live"],
                   help="Trading mode (default: mock)")
    p.add_argument("duration", nargs="?", type=float, default=60.0,
                   help="Run duration in seconds (default: 60)")
    p.add_argument("--pairs", nargs="+", default=["ETHUSD"],
                   help="Trading pairs (default: ETHUSD)")
    p.add_argument("--base-size", type=float, default=500.0,
                   help="Base trade size in USD (default: 500)")
    p.add_argument("--max-size", type=float, default=2000.0,
                   help="Max single trade size (default: 2000)")
    p.add_argument("--max-drawdown", type=float, default=200.0,
                   help="Max daily drawdown in USD (default: 200)")
    p.add_argument("--db", type=str, default="swarm.db",
                   help="SQLite database path")
    p.add_argument("--kill-switch", type=str, default="KILL",
                   help="Kill switch file path")
    p.add_argument("--ws", action="store_true",
                   help="Use WebSocket streaming instead of REST polling")
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="REST ticker poll interval in seconds (default: 2)")
    p.add_argument("--dashboard", action="store_true",
                   help="Show live terminal dashboard")
    p.add_argument("--no-advanced", action="store_true",
                   help="Disable advanced agents (orderbook, funding, spread)")
    return p.parse_args()


async def run(args: argparse.Namespace):
    load_dotenv()
    logging.basicConfig(
        level=logging.WARNING if args.dashboard else logging.INFO,
        format="%(asctime)s %(name)-16s %(levelname)-5s %(message)s",
    )
    log = logging.getLogger("swarm")
    log.setLevel(logging.INFO)
    log.info("Swarm Trade starting: mode=%s duration=%.0fs pairs=%s",
             args.mode, args.duration, args.pairs)

    bus = Bus()
    state: dict = {"daily_pnl": 0.0}
    kill_switch = Path(args.kill_switch)
    tasks_to_cancel = []
    stoppables = []

    assets = _pairs_to_assets(args.pairs)
    primary_asset = assets[0]

    # ── Data Scouts ─────────────────────────────────────────────
    if args.mode == "mock":
        scout = MockScout(bus, symbol=primary_asset, start=2200.0, interval=0.2)
    elif args.ws:
        ws_pairs = [p.replace("USD", "/USD") for p in args.pairs]
        scout = KrakenWSScout(bus, pairs=ws_pairs)
    else:
        scout = KrakenScout(bus, pairs=args.pairs, interval=args.poll_interval)

    stoppables.append(scout)
    tasks_to_cancel.append(asyncio.create_task(scout.run()))

    # ── Core Analysts (per asset) ───────────────────────────────
    for asset in assets:
        MomentumAnalyst(bus, asset=asset)
        MeanReversionAnalyst(bus, asset=asset)
        VolatilityAnalyst(bus, asset=asset)

    # ── Advanced Agents ─────────────────────────────────────────
    if args.mode != "mock" and not args.no_advanced:
        for i, (asset, pair) in enumerate(zip(assets, args.pairs)):
            # Order book agent
            ob = OrderBookAgent(bus, pair=pair, interval=5.0)
            stoppables.append(ob)
            tasks_to_cancel.append(asyncio.create_task(ob.run()))

            # Spread agent
            sp = SpreadAgent(bus, pair=pair, interval=10.0)
            stoppables.append(sp)
            tasks_to_cancel.append(asyncio.create_task(sp.run()))

            # Funding rate agent (only for primary asset to avoid API spam)
            if i == 0:
                futures_sym = ASSET_CONFIG.get(asset, {}).get("futures", f"PF_{asset}USD")
                fr = FundingRateAgent(bus, symbol=futures_sym, asset=asset, interval=60.0)
                stoppables.append(fr)
                tasks_to_cancel.append(asyncio.create_task(fr.run()))

        # Regime detector
        RegimeAgent(bus, asset=primary_asset, window=50)

    # ── PRISM AI Signals ────────────────────────────────────────
    if os.getenv("PRISM_API_KEY"):
        prism = PRISMSignalAgent(bus, assets=assets, interval=30.0)
        stoppables.append(prism)
        tasks_to_cancel.append(asyncio.create_task(prism.run()))
        log.info("PRISM AI signals enabled for: %s", assets)

    # ── Strategy ────────────────────────────────────────────────
    tokens = set(assets) | {"USD", "USDC", "USDT"}
    Strategist(bus, base_size=args.base_size)

    # ── Risk Agents ─────────────────────────────────────────────
    risks = [
        RiskAgent(bus, "size", size_check(max_size=args.max_size)),
        RiskAgent(bus, "allowlist", allowlist_check(tokens)),
        RiskAgent(bus, "drawdown", drawdown_check(state, max_dd=args.max_drawdown)),
    ]
    Coordinator(bus, n_risk_agents=len(risks))

    # ── Circuit Breakers ────────────────────────────────────────
    CircuitBreaker(
        bus, kill_switch,
        max_consecutive_losses=5,
        max_drawdown_usd=args.max_drawdown,
        vol_halt_threshold=0.05,
        cooldown_seconds=300.0,
    )
    if args.mode != "mock":
        PositionFlattener(bus, paper=(args.mode == "paper"))

    # ── Execution Pipeline ──────────────────────────────────────
    Simulator(bus)
    if args.mode == "mock":
        Executor(bus, kill_switch=kill_switch, dry_run=True)
    else:
        KrakenExecutor(bus, kill_switch=kill_switch, paper=(args.mode == "paper"))

    Auditor(bus, db_path=Path(args.db), state=state)

    # ── Dashboard ───────────────────────────────────────────────
    dash = None
    if args.dashboard:
        dash = Dashboard(bus, state, refresh=1.0)
        stoppables.append(dash)
        tasks_to_cancel.append(asyncio.create_task(dash.run()))

    # ── Run ──────────────────────────────────────────────────────
    try:
        await asyncio.sleep(args.duration)
    except asyncio.CancelledError:
        pass
    finally:
        for s in stoppables:
            s.stop()
        for t in tasks_to_cancel:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass

    log.info("FINAL daily_pnl=%+.4f trades=%d",
             state["daily_pnl"], state.get("trade_count", 0))

    # Print paper account summary
    if args.mode == "paper":
        try:
            import subprocess
            result = subprocess.run(
                ["kraken", "paper", "status", "-o", "json"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                log.info("Paper account: %s", result.stdout.strip())
        except Exception:
            pass


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(args))
