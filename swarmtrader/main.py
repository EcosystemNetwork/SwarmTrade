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
    # Rate limiting
    RateLimiter, rate_limit_check,
    # External signals
    PRISMSignalAgent,
    NewsAgent,
    WhaleAgent,
    # Market intelligence
    OpenInterestAgent, FearGreedAgent, SocialSentimentAgent,
    LiquidationAgent, OnChainAgent, ArbitrageAgent,
    # Intelligence
    MultiTimeframeMomentum,
    CorrelationAgent,
    ConfluenceDetector,
    # TA strategy agents
    RSIAgent, MACDAgent, BollingerAgent, VWAPAgent, IchimokuAgent,
    LiquidationCascadeAgent, ATRTrailingStopAgent, depth_liquidity_check,
    # Position management
    PositionManager, max_positions_check,
    TWAPExecutor,
    # Dashboard
    Dashboard,
    # Quantitative risk (Citadel-grade)
    VaREngine, var_check,
    StressTester, stress_check,
    # Smart order routing + microstructure
    SmartOrderRouter, sor_venue_check,
    IcebergExecutor, ExecutionQualityTracker,
    # ML signal
    MLSignalAgent, ml_model_check,
    # Factor model + PnL attribution
    FactorModel, PnLAttributor, FactorRiskModel, factor_exposure_check,
    # Portfolio optimization
    PortfolioOptAgent, rebalance_check,
    # Compliance + data quality
    WashTradingDetector, PositionLimitChecker, MarginMonitor,
    Reconciler, DataQualityMonitor, compliance_check,
    # Walk-forward + TCA
    TransactionCostAnalyzer,
)
from .automation import AgentSupervisor, build_scheduler
from .core import PortfolioTracker
from .safety import KillSwitch
from .wallet import WalletManager, funds_check, allocation_check
from .web import WebDashboard
from .gateway import AgentGateway


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
    p.add_argument("--web", action="store_true",
                   help="Launch web dashboard on http://localhost:8080")
    p.add_argument("--web-port", type=int, default=8080,
                   help="Web dashboard port (default: 8080)")
    p.add_argument("--max-positions", type=int, default=5,
                   help="Max simultaneous open positions (default: 5)")
    p.add_argument("--hard-stop", type=float, default=0.05,
                   help="Hard stop-loss per position as decimal (default: 0.05 = 5%%)")
    p.add_argument("--trail-stop", type=float, default=0.03,
                   help="Trailing stop as decimal (default: 0.03 = 3%%)")
    p.add_argument("--max-hold", type=float, default=3600.0,
                   help="Max hold time per position in seconds (default: 3600)")
    p.add_argument("--capital", type=float, default=10000.0,
                   help="Starting capital in USD (default: 10000)")
    p.add_argument("--max-alloc", type=float, default=50.0,
                   help="Max allocation per asset in %% (default: 50)")
    p.add_argument("--gateway", action="store_true",
                   help="Enable agent gateway for external AI agents (OpenClaw/Hermes)")
    p.add_argument("--gateway-key", type=str, default=None,
                   help="Master key for agent gateway (auto-generated if omitted)")
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
    state: dict = {"daily_pnl": 0.0, "trade_count": 0, "total_fees": 0.0}
    kill_switch = KillSwitch(Path(args.kill_switch))
    portfolio = PortfolioTracker()

    assets = _pairs_to_assets(args.pairs)
    primary_asset = assets[0]

    # ── Supervisor — monitors agent health, auto-restarts crashes ──
    supervisor = AgentSupervisor(bus, check_interval=5.0, max_restarts=3)

    # ── Wallet Manager ─────────────────────────────────────────
    alloc_cfg = {a: {"target_pct": 0.0, "max_pct": args.max_alloc} for a in assets}
    wallet = WalletManager(
        bus, portfolio,
        starting_capital=args.capital,
        db_path=Path(args.db),
        allocations=alloc_cfg,
    )
    log.info("Wallet: capital=%.0f max_alloc=%.0f%%", args.capital, args.max_alloc)

    # ── Data Scouts ─────────────────────────────────────────────
    if args.mode == "mock":
        scout = MockScout(bus, symbol=primary_asset, start=2200.0, interval=0.2)
    elif args.ws:
        ws_pairs = [p.replace("USD", "/USD") for p in args.pairs]
        scout = KrakenWSScout(bus, pairs=ws_pairs)
    else:
        scout = KrakenScout(bus, pairs=args.pairs, interval=args.poll_interval)

    supervisor.register("scout", scout.run, stale_after=10.0, stoppable=scout)

    # ── Core Analysts (per asset) ───────────────────────────────
    for asset in assets:
        MomentumAnalyst(bus, asset=asset)
        MeanReversionAnalyst(bus, asset=asset)
        VolatilityAnalyst(bus, asset=asset)

    # ── TA Strategy Agents (per asset) ─────────────────────────
    for asset in assets:
        RSIAgent(bus, asset=asset)          # 7-period, 75/25 thresholds
        MACDAgent(bus, asset=asset)         # 8/21/5 fast crypto params
        BollingerAgent(bus, asset=asset)    # 2.5 std for crypto vol
        VWAPAgent(bus, asset=asset)         # 120-tick weekly anchor
        IchimokuAgent(bus, asset=asset)

    # ── Research-Driven Agents (per asset) ─────────────────────
    for asset in assets:
        LiquidationCascadeAgent(bus, asset=asset)
        ATRTrailingStopAgent(bus, asset=asset)

    # ── Advanced Agents ─────────────────────────────────────────
    if args.mode != "mock" and not args.no_advanced:
        for i, (asset, pair) in enumerate(zip(assets, args.pairs)):
            # Order book agent
            ob = OrderBookAgent(bus, pair=pair, interval=5.0)
            supervisor.register(f"orderbook_{asset}", ob.run,
                                stale_after=15.0, stoppable=ob)

            # Spread agent
            sp = SpreadAgent(bus, pair=pair, interval=10.0)
            supervisor.register(f"spread_{asset}", sp.run,
                                stale_after=30.0, stoppable=sp)

            # Funding rate agent (only for primary asset to avoid API spam)
            if i == 0:
                futures_sym = ASSET_CONFIG.get(asset, {}).get("futures", f"PF_{asset}USD")
                fr = FundingRateAgent(bus, symbol=futures_sym, asset=asset, interval=60.0)
                supervisor.register(f"funding_{asset}", fr.run,
                                    stale_after=120.0, stoppable=fr)

        # Regime detector
        RegimeAgent(bus, asset=primary_asset, window=50)

    # ── Multi-Timeframe + Correlation Intelligence ───────────────
    for asset in assets:
        MultiTimeframeMomentum(bus, asset=asset)
    if len(assets) >= 2:
        ref = "BTC" if "BTC" in assets else assets[0]
        targets = [a for a in assets if a != ref]
        if targets:
            CorrelationAgent(bus, reference=ref, targets=targets)
            log.info("Correlation agent: %s → %s", ref, targets)

    # ── Confluence Detector ─────────────────────────────────────
    ConfluenceDetector(bus, min_groups=2)

    # ── News Sentiment ──────────────────────────────────────────
    if os.getenv("NEWS_API_KEY"):
        news_agent = NewsAgent(bus, assets=assets, interval=60.0)
        supervisor.register("news", news_agent.run,
                            stale_after=120.0, stoppable=news_agent)
        log.info("News sentiment enabled for: %s", assets)

    # ── Whale Tracking ──────────────────────────────────────────
    whale = WhaleAgent(bus, assets=assets, interval=120.0)
    supervisor.register("whale", whale.run, stale_after=300.0, stoppable=whale)

    # ── PRISM AI Signals ────────────────────────────────────────
    if os.getenv("PRISM_API_KEY"):
        prism = PRISMSignalAgent(bus, assets=assets, interval=30.0)
        supervisor.register("prism", prism.run,
                            stale_after=90.0, stoppable=prism)
        log.info("PRISM AI signals enabled for: %s", assets)

    # ── Open Interest (futures only) ──────────────────────────
    if args.mode != "mock" and not args.no_advanced:
        for asset in assets:
            futures_sym = ASSET_CONFIG.get(asset, {}).get("futures", f"PF_{asset}USD")
            oi = OpenInterestAgent(bus, symbol=futures_sym, asset=asset, interval=60.0)
            supervisor.register(f"oi_{asset}", oi.run, stale_after=180.0, stoppable=oi)
        log.info("Open Interest agents enabled for: %s", assets)

    # ── Fear & Greed Index ─────────────────────────────────────
    fg = FearGreedAgent(bus, assets=assets, interval=300.0)
    supervisor.register("fear_greed", fg.run, stale_after=600.0, stoppable=fg)

    # ── Social Sentiment ───────────────────────────────────────
    social = SocialSentimentAgent(bus, assets=assets, interval=300.0)
    supervisor.register("social", social.run, stale_after=600.0, stoppable=social)

    # ── Liquidation Levels (futures only) ──────────────────────
    if args.mode != "mock" and not args.no_advanced:
        for asset in assets:
            futures_sym = ASSET_CONFIG.get(asset, {}).get("futures", f"PF_{asset}USD")
            liq = LiquidationAgent(bus, asset=asset, futures_symbol=futures_sym, interval=30.0)
            supervisor.register(f"liq_{asset}", liq.run, stale_after=120.0, stoppable=liq)
        log.info("Liquidation agents enabled for: %s", assets)

    # ── On-Chain Activity ──────────────────────────────────────
    oc = OnChainAgent(bus, assets=assets, interval=300.0)
    supervisor.register("onchain", oc.run, stale_after=600.0, stoppable=oc)

    # ── Cross-Exchange Arbitrage ───────────────────────────────
    arb = ArbitrageAgent(bus, assets=assets, interval=120.0)
    supervisor.register("arbitrage", arb.run, stale_after=300.0, stoppable=arb)

    # ── ML Signal Agent (per asset) ────────────────────────────
    for asset in assets:
        MLSignalAgent(bus, asset=asset, retrain_interval=500, min_samples=200)
    log.info("ML signal agents enabled for: %s", assets)

    # ── Strategy ────────────────────────────────────────────────
    tokens = set(assets) | {"USD", "USDC", "USDT"}
    strategist = Strategist(bus, base_size=args.base_size, portfolio=portfolio)

    # ── Position Management ───────────────────────────────────────
    pos_mgr = PositionManager(
        bus,
        trail_pct=args.trail_stop,
        hard_stop_pct=args.hard_stop,
        max_hold=args.max_hold,
        max_positions=args.max_positions,
        max_exposure_per_asset=args.max_size * 2,
        max_total_exposure=args.max_size * 5,
    )

    # ── Quantitative Risk — VaR Engine ─────────────────────────
    var_engine = VaREngine(bus, portfolio=portfolio)
    supervisor.register("var_engine", var_engine.run, stale_after=60.0)
    log.info("VaR engine enabled (historical + parametric + Monte Carlo)")

    # ── Stress Testing ─────────────────────────────────────────
    stress_tester = StressTester(portfolio=portfolio)

    # ── Factor Model + PnL Attribution ─────────────────────────
    factor_model = FactorModel(bus)
    pnl_attributor = PnLAttributor(bus, portfolio=portfolio, factor_model=factor_model)  # noqa: F841 — event-driven, no run() loop
    factor_risk = FactorRiskModel(factor_model=factor_model, portfolio=portfolio)
    log.info("Factor model + PnL attribution enabled")

    # ── Portfolio Optimization ─────────────────────────────────
    portfolio_opt = PortfolioOptAgent(
        bus, portfolio=portfolio,
        optimizer="risk_parity", rebalance_interval=60.0,
    )
    portfolio_opt.attach_hedger(primary_asset=primary_asset)
    supervisor.register("portfolio_opt", portfolio_opt.run, stale_after=120.0)
    log.info("Portfolio optimization (risk parity) + dynamic hedger enabled")

    # ── Smart Order Router ─────────────────────────────────────
    sor = SmartOrderRouter(bus)
    supervisor.register("sor", sor.run, stale_after=15.0)
    log.info("Smart order router: 5 venues (Kraken, Binance, Coinbase, OKX, dYdX)")

    # ── Microstructure — Iceberg + TCA ─────────────────────────
    iceberg = IcebergExecutor(bus, threshold_usd=args.max_size * 0.75)
    tca = ExecutionQualityTracker(bus)
    supervisor.register("tca", tca.run, stale_after=60.0)
    log.info("Iceberg executor + TCA tracker enabled")

    # ── Compliance — Wash Trading + Margin + Data Quality ──────
    wash_detector = WashTradingDetector(bus)
    pos_limit_checker = PositionLimitChecker(
        portfolio=portfolio, per_asset_limit=args.max_size * 3,
        total_limit=args.capital * 0.8,
    )
    margin_monitor = MarginMonitor(bus, portfolio=portfolio, total_capital=args.capital)
    reconciler = Reconciler(portfolio=portfolio)
    data_quality = DataQualityMonitor(bus)
    supervisor.register("reconciler", reconciler.run, stale_after=120.0)
    supervisor.register("data_quality", data_quality.run, stale_after=30.0)
    log.info("Compliance suite enabled (wash trade, margin, reconciliation, data quality)")

    # ── Transaction Cost Analyzer ──────────────────────────────
    tca_analyzer = TransactionCostAnalyzer(bus)
    supervisor.register("tca_analyzer", tca_analyzer.run, stale_after=60.0)

    # ── Risk Agents ─────────────────────────────────────────────
    rate_limiter = RateLimiter(bus, max_trades=20, window_s=3600.0)
    risks = [
        RiskAgent(bus, "size", size_check(max_size=args.max_size)),
        RiskAgent(bus, "allowlist", allowlist_check(tokens)),
        RiskAgent(bus, "drawdown", drawdown_check(state, max_dd=args.max_drawdown)),
        RiskAgent(bus, "rate_limit", rate_limit_check(rate_limiter)),
        RiskAgent(bus, "positions", max_positions_check(pos_mgr)),
        RiskAgent(bus, "funds", funds_check(wallet)),
        RiskAgent(bus, "allocation", allocation_check(wallet)),
        RiskAgent(bus, "depth", depth_liquidity_check(bus, min_depth_ratio=2.0)),
        # Citadel-grade risk checks
        RiskAgent(bus, "var", var_check(var_engine, max_var=args.capital * 0.05)),
        RiskAgent(bus, "stress", stress_check(stress_tester, max_stress_loss_pct=0.30)),
        RiskAgent(bus, "compliance", compliance_check(wash_detector, margin_monitor)),
        RiskAgent(bus, "factor_exposure", factor_exposure_check(factor_model, max_exposure=3.0)),
        RiskAgent(bus, "rebalance", rebalance_check(portfolio_opt, max_drift_pct=0.15)),
        RiskAgent(bus, "sor_venues", sor_venue_check(sor, min_venues=2)),
    ]
    Coordinator(bus, n_risk_agents=len(risks))

    # ── Circuit Breakers ────────────────────────────────────────
    cb = CircuitBreaker(
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
        Executor(bus, kill_switch=kill_switch, dry_run=True, portfolio=portfolio)
    else:
        KrakenExecutor(bus, kill_switch=kill_switch, paper=(args.mode == "paper"),
                       portfolio=portfolio)

    Auditor(bus, db_path=Path(args.db), state=state, portfolio=portfolio)

    # ── TWAP Execution (splits large orders) ───────────────────
    TWAPExecutor(bus, threshold=args.max_size * 0.75, n_slices=5, window_s=60.0)

    # ── Terminal Dashboard ──────────────────────────────────────
    if args.dashboard:
        dash = Dashboard(bus, state, refresh=1.0)
        supervisor.register("dashboard", dash.run, stale_after=5.0, stoppable=dash)

    # ── Agent Gateway ──────────────────────────────────────────
    gateway = None
    if args.gateway or os.getenv("SWARM_GATEWAY"):
        master_key = args.gateway_key or os.getenv("SWARM_GATEWAY_KEY")
        gateway = AgentGateway(
            bus, strategist=strategist, portfolio=portfolio,
            master_key=master_key,
        )
        log.info("Agent Gateway enabled — external agents can connect")
        log.info("  master_key=%s...", gateway.master_key[:12])
        if not args.web:
            # Launch standalone gateway server
            from aiohttp import web as aweb
            gw_app = aweb.Application()
            gateway.register_routes(gw_app)
            runner = aweb.AppRunner(gw_app)
            await runner.setup()
            gw_port = args.web_port + 1  # 8081 by default
            site = aweb.TCPSite(runner, "0.0.0.0", gw_port)
            await site.start()
            log.info("  Gateway API: http://localhost:%d/api/gateway/connect", gw_port)
            log.info("  Gateway WS:  ws://localhost:%d/ws/agent", gw_port)

    # ── Web Dashboard ──────────────────────────────────────────
    if args.web:
        web_dash = WebDashboard(
            bus, state, db_path=Path(args.db),
            kill_switch=kill_switch, port=args.web_port,
            wallet=wallet, gateway=gateway,
        )
        await web_dash.start()
        log.info("Web dashboard: http://localhost:%d", args.web_port)
        if gateway:
            log.info("  Gateway API: http://localhost:%d/api/gateway/connect", args.web_port)

    # ── Scheduler — periodic automation tasks ───────────────────
    scheduler = build_scheduler(
        bus,
        supervisor=supervisor,
        circuit_breaker=cb,
        strategist=strategist,
        portfolio=portfolio,
        state=state,
    )

    # ── Launch all supervised agents + scheduler ─────────────────
    sup_tasks = supervisor.start_all()
    sched_task = asyncio.create_task(scheduler.run())
    health_task = asyncio.create_task(supervisor.run())

    agent_count = len(supervisor._agents)
    log.info("SWARM ONLINE: %d supervised agents, scheduler active", agent_count)
    await bus.publish("automation.startup", {
        "agents": list(supervisor._agents.keys()),
        "mode": args.mode,
        "assets": assets,
    })

    # ── Run ──────────────────────────────────────────────────────
    try:
        await asyncio.sleep(args.duration)
    except asyncio.CancelledError:
        pass
    finally:
        log.info("SWARM SHUTDOWN starting...")
        scheduler.stop()
        sched_task.cancel()
        supervisor.stop_all()
        health_task.cancel()
        all_tasks = sup_tasks + [sched_task, health_task]
        for t in all_tasks:
            if not t.done():
                t.cancel()
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        log.info("SWARM SHUTDOWN complete")

    log.info("FINAL daily_pnl=%+.4f trades=%d equity=%.4f fees=%.4f",
             state["daily_pnl"], state.get("trade_count", 0),
             portfolio.total_equity(), portfolio.total_fees())
    import json as _json
    log.info("POSITIONS %s", _json.dumps(pos_mgr.summary(), indent=2))
    log.info("WALLET %s", _json.dumps(wallet.summary(), indent=2))

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
