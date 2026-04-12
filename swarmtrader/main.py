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
    MockScout, KrakenScout,
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
    # Extended data feeds
    ExchangeFlowAgent, StablecoinAgent, MacroCalendarAgent,
    DeribitOptionsAgent, TokenUnlockAgent, GitHubDevAgent, RSSNewsAgent,
)
from .automation import AgentSupervisor, build_scheduler
from .core import PortfolioTracker
from .safety import KillSwitch
from .wallet import WalletManager, funds_check, allocation_check
from .web import WebDashboard
from .gateway import AgentGateway
from .checkpoint import Checkpoint
from .database import Database
from .demo import DemoScout, MultiAssetDemoScout
from .memory import AgentMemory
from .erc8004 import ERC8004Pipeline
from .uniswap import UniswapExecutor, uniswap_venue_config
# Moon Dev tech
from .hyperliquid import HyperliquidAgent, HyperliquidExecutor
from .backtester import BacktesterAgent
from .jupiter import JupiterExecutor, JupiterPriceScout
from .birdeye import BirdEyeAgent
from .social_agents import XMonitorAgent, DiscordAgent, TelegramAgent, SocialAggregator
from .agent_learning import LearningCoordinator
from .smart_money import SmartMoneyAgent
from .x402_payments import X402PaymentGateway
from .lp_manager import LPRebalanceManager
from .yield_aggregator import YieldAggregator
from .agent_policies import AgentPolicyEngine, AgentPolicy, policy_check
from .hardware_signer import HardwareSigningPipeline
from .polymarket import PolymarketAgent
from .alpha_swarm import setup_alpha_swarm
from .price_gate import PriceValidationGate
from .kalman import AdaptiveKalmanFilter
from .fusion import DataFusionPipeline
from .debate import setup_debate_engine, ELOTracker
from .agent_policies import ExecutionSandbox
from .vault import VaultManager, vault_trade_check
from .marketplace import AgentMarketplace
from .flashloan import FlashLoanExecutor
from .strategy_nft import StrategyNFTManager
from .v4_hooks import V4HookManager
from .cross_chain import CrossChainCoordinator
from .zk_trading import CommitRevealEngine
from .rugpull_detector import RugpullDetector
from .prediction_trader import PredictionTrader
from .agent_payments import AgentPaymentProtocol
from .strategy_evolution import StrategyEvolution
from .ai_brain import AIBrain
from .narrative import NarrativeEngine
from .whale_mirror import WhaleMirrorAgent
from .intent_solver import IntentSolverNetwork
from .liquidation_shield import LiquidationShield
from .governance import GovernanceDAO
from .agent_registry import AgentRegistry
from .arb_executor import ArbScanner, ArbExecutor
from .dex_quotes import DEXQuoteProvider
from .dex_multi import MultiDEXScanner
from .hermes_brain import HermesBrain


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
        description="Swarm Trade \u2014 AI trading agent swarm",
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
                   help="SQLite fallback path (ignored when DATABASE_URL is set)")
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
    p.add_argument("--web-host", type=str,
                   default=os.getenv("SWARM_WEB_HOST", "127.0.0.1"),
                   help="Web dashboard bind address (default: 127.0.0.1, use 0.0.0.0 for Docker)")
    p.add_argument("--web-port", type=int,
                   default=int(os.getenv("PORT", "8080")),
                   help="Web dashboard port (default: $PORT or 8080)")
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
    p.add_argument("--demo", action="store_true",
                   help="Use demo replay mode with pre-recorded market scenario")
    p.add_argument("--hermes", action="store_true",
                   help="Use local Hermes LLM (via Ollama) as the brain instead of rule-based Strategist")
    p.add_argument("--checkpoint", action="store_true",
                   help="Enable state checkpointing for crash recovery")
    p.add_argument("--checkpoint-path", type=str, default="swarm_checkpoint.json",
                   help="Checkpoint file path (default: swarm_checkpoint.json)")
    p.add_argument("--erc8004", action="store_true",
                   help="Enable ERC-8004 on-chain identity, reputation, and validation")
    p.add_argument("--erc8004-network", type=str, default="sepolia",
                   choices=["sepolia", "mainnet", "base"],
                   help="ERC-8004 network (default: sepolia)")
    return p.parse_args()


def _validate_env_config(log):
    """Validate environment variable types and ranges at startup. Fail fast on bad config."""
    errors = []

    def _check_int(name, min_val=None, max_val=None):
        val = os.getenv(name)
        if val is None:
            return
        try:
            n = int(val)
            if min_val is not None and n < min_val:
                errors.append(f"{name}={val} must be >= {min_val}")
            if max_val is not None and n > max_val:
                errors.append(f"{name}={val} must be <= {max_val}")
        except ValueError:
            errors.append(f"{name}={val!r} is not a valid integer")

    def _check_float(name, min_val=None, max_val=None):
        val = os.getenv(name)
        if val is None:
            return
        try:
            f = float(val)
            if min_val is not None and f < min_val:
                errors.append(f"{name}={val} must be >= {min_val}")
            if max_val is not None and f > max_val:
                errors.append(f"{name}={val} must be <= {max_val}")
        except ValueError:
            errors.append(f"{name}={val!r} is not a valid float")

    # Database
    _check_int("SWARM_DB_MIN_POOL", min_val=1, max_val=50)
    _check_int("SWARM_DB_MAX_POOL", min_val=1, max_val=100)
    # Price gate
    _check_int("PRICE_GATE_SAFE_BPS", min_val=1, max_val=1000)
    _check_int("PRICE_GATE_WARN_BPS", min_val=1, max_val=5000)
    _check_float("PRICE_GATE_RETRY_DELAY", min_val=0.1, max_val=60)
    _check_int("PRICE_GATE_MAX_RETRIES", min_val=0, max_val=10)
    _check_float("PRICE_GATE_STALE_SECS", min_val=5, max_val=300)
    # Web
    _check_int("PORT", min_val=1, max_val=65535)

    if errors:
        for err in errors:
            log.error("CONFIG ERROR: %s", err)
        raise SystemExit(
            f"Startup aborted: {len(errors)} config error(s). Check env vars."
        )
    log.info("Environment config validated OK")


async def run(args: argparse.Namespace):
    load_dotenv()
    # Expose mode to submodules (e.g. web.py token enforcement)
    os.environ["SWARM_MODE"] = args.mode
    from .logging_config import setup_logging
    setup_logging(
        json_mode=(args.mode != "mock"),
        level="WARNING" if args.dashboard else "INFO",
        log_file="trading.log" if args.mode != "mock" else None,
    )
    log = logging.getLogger("swarm")
    log.setLevel(logging.INFO)
    log.info("Swarm Trade starting: mode=%s duration=%.0fs pairs=%s",
             args.mode, args.duration, args.pairs)

    # Pre-flight: validate environment config
    _validate_env_config(log)

    # Pre-flight: live mode safety gate
    if args.mode == "live":
        confirm = os.getenv("SWARM_LIVE_CONFIRM", "")
        if confirm != "I_ACCEPT_RISK":
            log.critical("ABORTING: live mode requires SWARM_LIVE_CONFIRM=I_ACCEPT_RISK")
            raise SystemExit(
                "Live trading BLOCKED. Set SWARM_LIVE_CONFIRM=I_ACCEPT_RISK to confirm "
                "you understand this bot will execute real trades with real money."
            )
        log.warning("LIVE MODE ENABLED — real orders will execute on Kraken")

    # Pre-flight: validate API keys before entering live/paper mode
    if args.mode in ("live", "paper"):
        from .kraken import validate_api_keys
        ok, msg = await validate_api_keys()
        if not ok:
            if args.mode == "live":
                log.critical("ABORTING: %s", msg)
                raise SystemExit(f"API key validation failed: {msg}")
            else:
                log.warning("API key validation: %s (continuing in paper mode)", msg)

    bus = Bus()
    state: dict = {"daily_pnl": 0.0, "trade_count": 0, "total_fees": 0.0}
    kill_switch = KillSwitch(Path(args.kill_switch))
    portfolio = PortfolioTracker()

    assets = _pairs_to_assets(args.pairs)
    primary_asset = assets[0]

    # ── Database ───────────────────────────────────────────────
    database_url = os.getenv("DATABASE_URL", "")
    db = Database(
        database_url=database_url or None,
        sqlite_path=Path(args.db) if not database_url else None,
    )
    await db.connect()
    backend = "Postgres (Neon)" if db.is_postgres else f"SQLite ({args.db})"
    log.info("Database: %s", backend)

    # ── Supervisor — monitors agent health, auto-restarts crashes ──
    supervisor = AgentSupervisor(bus, check_interval=5.0, max_restarts=3)

    # ── Wallet Manager ─────────────────────────────────────────
    alloc_cfg = {a: {"target_pct": 0.0, "max_pct": args.max_alloc} for a in assets}
    wallet = WalletManager(
        bus, portfolio,
        starting_capital=args.capital,
        db=db,
        allocations=alloc_cfg,
    )
    await wallet.load_state()
    log.info("Wallet: capital=%.0f max_alloc=%.0f%%", args.capital, args.max_alloc)

    # ── Agent Memory — cross-session learning ──────────────────
    memory = AgentMemory(
        bus, portfolio, state,
        soul_path=Path("SOUL.md"),
        notes_path=Path("strategy_notes.md"),
        session_log_path=Path("session_log.json"),
    )
    soul = memory.read_soul()
    if soul:
        log.info("SOUL loaded: %d chars", len(soul))
    past_notes = memory.read_notes()
    if past_notes:
        log.info("Strategy notes loaded: %d lines from prior sessions",
                 past_notes.count("\n"))
    past_sessions = memory.get_past_sessions(limit=5)
    if past_sessions:
        last = past_sessions[-1]
        log.info("Last session: PnL=%s trades=%s WR=%s",
                 last.get("total_pnl"), last.get("trades_total"),
                 last.get("win_rate"))

    # ── Checkpoint — restore state from previous run ──────────
    ckpt = None
    if getattr(args, "checkpoint", False):
        ckpt = Checkpoint(
            bus, portfolio, state,
            path=Path(getattr(args, "checkpoint_path", "swarm_checkpoint.json")),
            wallet=wallet,
        )
        if ckpt.restore():
            log.info("Resumed from checkpoint")

    # ── Kraken REST Client (shared) ──────────────────────────────
    from .kraken_api import get_client, KrakenAPIConfig as _KrakenCfg
    kraken_client = get_client(_KrakenCfg(
        api_key=os.getenv("KRAKEN_API_KEY", ""),
        api_secret=os.getenv("KRAKEN_PRIVATE_KEY", ""),
        tier=os.getenv("KRAKEN_TIER", "starter"),
    ))

    # ── WebSocket v2 Client (shared) ──────────────────────────────
    ws_v2_client = None
    if args.ws and args.mode != "mock":
        from .kraken_ws import KrakenWSv2Client
        ws_v2_client = KrakenWSv2Client(bus, client=kraken_client)
        ws_pairs = [p.replace("USD", "/USD") for p in args.pairs]
        ws_v2_client.subscribe_ticker(ws_pairs)
        ws_v2_client.subscribe_book(ws_pairs, depth=25)
        ws_v2_client.subscribe_trades(ws_pairs)
        supervisor.register("ws_public", ws_v2_client.run_public,
                            stale_after=30.0)
        log.info("Kraken WS v2 (public): ticker + book + trades for %s", ws_pairs)

        # Private channels for paper/live (executions feed)
        if os.getenv("KRAKEN_API_KEY"):
            ws_v2_client.subscribe_executions()
            supervisor.register("ws_private", ws_v2_client.run_private,
                                stale_after=60.0)
            log.info("Kraken WS v2 (private): executions channel enabled")

    # ── Pyth Oracle (decentralized price redundancy) ──────────────
    from .pyth_oracle import PythOracle
    pyth = PythOracle(bus, assets=assets, interval=10.0)
    supervisor.register("pyth_oracle", pyth.run, stale_after=30.0, stoppable=pyth)
    log.info("Pyth oracle: decentralized price feeds for %s", assets)

    # ── Data Scouts ─────────────────────────────────────────────
    if getattr(args, "demo", False):
        if len(assets) > 1:
            scout = MultiAssetDemoScout(bus, assets=assets, interval=0.3)
        else:
            scout = DemoScout(bus, symbol=primary_asset, start_price=2200.0, interval=0.3)
        log.info("DEMO MODE: replaying pre-recorded market scenario")
    elif args.mode == "mock":
        scout = MockScout(bus, symbol=primary_asset, start=2200.0, interval=0.2)
    elif args.ws and ws_v2_client:
        # WS v2 client already registered above — use a lightweight scout as fallback
        scout = KrakenScout(bus, pairs=args.pairs, interval=args.poll_interval,
                            client=kraken_client)
        log.info("KrakenScout (REST fallback alongside WS v2)")
    else:
        scout = KrakenScout(bus, pairs=args.pairs, interval=args.poll_interval,
                            client=kraken_client)

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
    # RSSNewsAgent is the primary free news source (no key needed).
    # If NEWS_API_KEY is set, CryptoPanic NewsAgent runs alongside.
    if os.getenv("NEWS_API_KEY"):
        news_agent = NewsAgent(bus, assets=assets, interval=60.0)
        supervisor.register("news_cryptopanic", news_agent.run,
                            stale_after=120.0, stoppable=news_agent)
        log.info("CryptoPanic news sentiment enabled for: %s", assets)

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

    # ── Cross-Exchange Arbitrage (monitoring via CoinGecko) ────
    arb = ArbitrageAgent(bus, assets=assets, interval=120.0)
    supervisor.register("arbitrage", arb.run, stale_after=300.0, stoppable=arb)

    # ── Multi-Venue Arb Scanner + Executor (CEX + DEX) ────────
    # DEX quote provider (1inch + Jupiter)
    dex_provider = DEXQuoteProvider(
        bus, chain_id=int(os.getenv("DEX_CHAIN_ID", "8453")),  # Base by default
        api_key=os.getenv("ONEINCH_API_KEY", ""),
        interval=15.0,
    )
    supervisor.register("dex_quotes", dex_provider.run,
                        stale_after=60.0, stoppable=dex_provider)

    # Arb scanner — polls all CEXs + DEXs for price discrepancies
    arb_scanner = ArbScanner(
        bus, assets=assets, interval=10.0,
        min_spread_bps=15.0,  # only arb if > 15bps net profit
        max_arb_usd=float(os.getenv("SWARM_ARB_MAX_USD", "2000")),
        dex_provider=dex_provider,
    )
    arb_scanner._kraken_client = kraken_client
    supervisor.register("arb_scanner", arb_scanner.run,
                        stale_after=30.0, stoppable=arb_scanner)

    # Arb executor — simultaneously buys cheap + sells expensive
    arb_exec = ArbExecutor(
        bus, kraken_client=kraken_client,
        max_concurrent=2, cooldown_seconds=30.0,
        max_size_usd=float(os.getenv("SWARM_ARB_MAX_USD", "1000")),
        kill_switch=kill_switch,
    )

    log.info("Multi-venue arbitrage: scanner(10s) + executor(CEX+DEX) + DEX quotes(1inch+Jupiter)")

    # ── Multi-DEX Scanner (SushiSwap, Aerodrome, Curve, PancakeSwap, Raydium, Orca) ──
    dex_chains = [int(c) for c in os.getenv("DEX_CHAINS", "1,8453,42161").split(",")]
    multi_dex = MultiDEXScanner(
        bus, assets=assets, chains=dex_chains, interval=30.0,
    )
    supervisor.register("multi_dex", multi_dex.run,
                        stale_after=90.0, stoppable=multi_dex)
    log.info("Multi-DEX scanner: SushiSwap, Aerodrome, Curve, PancakeSwap, Raydium, Orca across chains %s", dex_chains)

    # ── Extended Data Feeds ───────────────────────────────────

    # Exchange net flow (volume-based reserve proxy via CoinGecko)
    exflow = ExchangeFlowAgent(bus, assets=assets, interval=300.0)
    supervisor.register("exchange_flow", exflow.run, stale_after=600.0, stoppable=exflow)

    # Stablecoin health (USDT/USDC market cap + depeg detection)
    stable = StablecoinAgent(bus, assets=assets, interval=600.0)
    supervisor.register("stablecoin", stable.run, stale_after=1200.0, stoppable=stable)

    # Macro economic calendar (FOMC, CPI, NFP from free APIs)
    macro = MacroCalendarAgent(bus, assets=assets, interval=1800.0)
    supervisor.register("macro", macro.run, stale_after=3600.0, stoppable=macro)

    # Options flow (Deribit put/call ratio, IV, skew — BTC/ETH only)
    options_assets = [a for a in assets if a in ("BTC", "ETH")]
    if options_assets:
        opts = DeribitOptionsAgent(bus, assets=options_assets, interval=300.0)
        supervisor.register("options", opts.run, stale_after=600.0, stoppable=opts)
        log.info("Options flow (Deribit) enabled for: %s", options_assets)

    # Token unlock / TVL monitoring (DeFi Llama)
    unlock = TokenUnlockAgent(bus, assets=assets, interval=3600.0)
    supervisor.register("unlock", unlock.run, stale_after=7200.0, stoppable=unlock)

    # GitHub developer activity (commit velocity, releases)
    ghdev = GitHubDevAgent(bus, assets=assets, interval=1800.0)
    supervisor.register("github", ghdev.run, stale_after=3600.0, stoppable=ghdev)

    # RSS multi-source news (CoinDesk, CoinTelegraph, Decrypt — no API key)
    rss = RSSNewsAgent(bus, assets=assets, interval=300.0)
    supervisor.register("rss_news", rss.run, stale_after=600.0, stoppable=rss)

    log.info("Extended feeds: exchange_flow, stablecoin, macro, unlock, github, rss_news")

    # ── ML Signal Agent (per asset) ────────────────────────────
    for asset in assets:
        MLSignalAgent(bus, asset=asset, retrain_interval=500, min_samples=200)
    log.info("ML signal agents enabled for: %s", assets)

    # ── Smart Money Wallet Tracking (Shadow pattern) ──────────
    smart_money = SmartMoneyAgent(bus, assets=assets, interval=180.0)
    supervisor.register("smart_money", smart_money.run, stale_after=360.0, stoppable=smart_money)
    log.info("Smart money tracking: %d wallets for %s", len(smart_money.wallets), assets)

    # ── Polymarket Prediction Market Signals ──────────────────
    polymarket = PolymarketAgent(bus, assets=assets, interval=300.0)
    supervisor.register("polymarket", polymarket.run, stale_after=600.0, stoppable=polymarket)
    log.info("Polymarket prediction signals enabled for: %s", assets)

    # ── Yield Aggregator (DeFi Llama + auto-compound) ─────────
    yield_agg = YieldAggregator(
        bus, assets=["ETH", "USDC", "WBTC"],
        chains=["Ethereum", "Base", "Arbitrum"],
        min_apy=2.0, scan_interval=600.0, harvest_interval=3600.0,
    )
    supervisor.register("yield_agg", yield_agg.run, stale_after=1200.0, stoppable=yield_agg)
    log.info("Yield aggregator: scanning DeFi Llama for top pools")

    # ── Adaptive Kalman Filter (signal noise removal) ─────────
    kalman_filter = AdaptiveKalmanFilter(bus, min_confidence_boost=0.1)
    log.info("Kalman filter: adaptive noise removal on all signal topics")

    # ── Data Fusion Pipeline (cross-source convergence) ──────
    fusion = DataFusionPipeline(
        bus, window_s=120.0, min_sources=2, fusion_interval=30.0,
    )
    log.info("Data fusion: convergence scoring across %d signal sources", len(fusion._buckets))

    # ── Alpha Swarm (multi-agent opportunity detection) ───────
    alpha_components = setup_alpha_swarm(
        bus, min_agents_agree=3, window_s=300.0, min_conviction=0.4,
    )
    log.info("Alpha swarm: hunter -> sentiment -> risk -> coordinator")

    # ── Adversarial Debate Engine (bull vs bear before trades) ─
    elo_tracker = ELOTracker()
    debate_engine = setup_debate_engine(
        bus, elo=elo_tracker, min_margin=0.3, memory_depth=3,
    )
    log.info("Debate engine: bull vs bear adversarial validation with ELO reputation")

    # ── LP Rebalance Manager (UniRange pattern) ───────────────
    lp_mgr = LPRebalanceManager(
        bus, range_bps=500, rebalance_threshold=0.8,
        min_profit_usd=5.0, check_interval=60.0,
    )
    supervisor.register("lp_manager", lp_mgr.run, stale_after=120.0, stoppable=lp_mgr)
    log.info("LP rebalance manager: %dbps range, auto-rebalance at %.0f%%",
             lp_mgr.range_bps, lp_mgr.rebalance_threshold * 100)

    # ── Agent Policy Engine (per-agent spending limits) ───────
    policy_engine = AgentPolicyEngine(bus, default_daily_cap=args.capital * 0.1)
    log.info("Agent policy engine: default daily cap=$%.0f", args.capital * 0.1)

    # ── Execution Sandbox (agents trade but can't withdraw) ──
    sandbox = ExecutionSandbox()
    # All internal agents get TRADE_ONLY by default
    for agent_name in ["momentum", "mean_rev", "rsi", "macd", "bollinger",
                        "vwap", "ichimoku", "whale", "news", "ml",
                        "alpha_swarm", "debate_engine", "fusion",
                        "kalman", "smart_money", "sentiment"]:
        sandbox.set_permission(agent_name, ExecutionSandbox.TRADE_ONLY)
    log.info("Execution sandbox: %d agents sandboxed to TRADE_ONLY", len(sandbox._agent_permissions))

    # ── ERC-4626 Vault Manager (standard fund custody) ─────────
    vault_mode = os.getenv("VAULT_MODE", "simulate")
    vault = VaultManager(bus, mode=vault_mode, daily_limit=args.capital * 5)
    # Seed vault with initial capital in simulate mode
    if vault_mode == "simulate" and args.capital > 0:
        vault.deposit("system", args.capital)
        vault.add_allowed_token("ETH")
        vault.add_allowed_token("BTC")
        vault.add_allowed_token("WETH")
    log.info("ERC-4626 vault: mode=%s assets=$%.0f daily_limit=$%.0f",
             vault_mode, vault.state.total_assets, vault.state.daily_limit)

    # ── Agent Marketplace (competitive signal auctions) ──────
    marketplace = AgentMarketplace(
        bus, auction_window_s=10.0, min_bids=1, elo_tracker=elo_tracker,
    )
    log.info("Agent marketplace: competitive signal auctions (window=10s)")

    # ── Flash Loan Arbitrage Engine ──────────────────────────
    flash_mode = os.getenv("FLASHLOAN_MODE", "simulate")
    flashloan = FlashLoanExecutor(
        bus, mode=flash_mode,
        min_profit_usd=float(os.getenv("FLASHLOAN_MIN_PROFIT_USD", "5.0")),
        cooldown_s=float(os.getenv("FLASHLOAN_COOLDOWN_S", "60")),
    )
    log.info("Flash loan arb: mode=%s min_profit=$%.1f", flash_mode, flashloan.simulator.min_profit_usd)

    # ── Strategy-as-NFT Manager ──────────────────────────────
    strategy_nft = StrategyNFTManager(bus, mode=os.getenv("STRATEGY_NFT_MODE", "simulate"))
    log.info("Strategy NFT: mintable/forkable strategies with royalty chain")

    # ── Uniswap v4 Hook Manager ──────────────────────────────
    v4_mode = os.getenv("V4_MODE", "simulate")
    v4_hooks = V4HookManager(bus, mode=v4_mode)
    # Register default pools
    v4_hooks.register_pool("eth-usdc-30", "ETH", "USDC", fee_bps=30)
    v4_hooks.register_pool("btc-usdc-30", "BTC", "USDC", fee_bps=30)
    log.info("V4 hooks: %d pools monitored (mode=%s)", len(v4_hooks._pools), v4_mode)

    # ── Cross-Chain Coordinator ──────────────────────────────
    cc_chains = [int(c) for c in os.getenv("CROSSCHAIN_CHAINS", "8453,42161,10").split(",")]
    crosschain = CrossChainCoordinator(
        bus, chains=cc_chains,
        min_yield_diff=float(os.getenv("CROSSCHAIN_MIN_YIELD_DIFF", "2.0")),
        max_rebalance_pct=float(os.getenv("CROSSCHAIN_REBALANCE_PCT", "25")),
    )
    log.info("Cross-chain: monitoring %d chains for yield optimization", len(cc_chains))

    # ── ZK Private Trading (commit-reveal dark pool) ─────────
    zk_engine = CommitRevealEngine(bus)
    log.info("ZK trading: commit-reveal dark pool with batch matching")

    # ── Phase 11: Rugpull/Scam Detection ─────────────────────
    rugpull = RugpullDetector(
        bus, mode=os.getenv("RUGPULL_MODE", "strict"),
        min_age_days=float(os.getenv("RUGPULL_MIN_AGE_DAYS", "7")),
        min_liquidity_usd=float(os.getenv("RUGPULL_MIN_LIQ_USD", "50000")),
    )
    log.info("Rugpull detector: mode=%s min_age=%dd min_liq=$%.0f",
             rugpull.mode, rugpull.min_age_days, rugpull.min_liquidity_usd)

    # ── Phase 12: Prediction Market Trading ──────────────────
    prediction = PredictionTrader(
        bus, mode=os.getenv("POLYMARKET_MODE", "simulate"),
        kelly_frac=float(os.getenv("PREDICTION_KELLY_FRAC", "0.15")),
        min_edge=float(os.getenv("PREDICTION_MIN_EDGE", "0.05")),
    )
    log.info("Prediction trader: mode=%s kelly=%.2f min_edge=%.1f%%",
             prediction.mode, prediction.kelly_frac, prediction.min_edge * 100)

    # ── Phase 13: Agent Payment Protocol ─────────────────────
    agent_pay = AgentPaymentProtocol(bus, default_budget=args.capital * 0.01)
    log.info("Agent payments: micropayment protocol with batch settlement")

    # ── Phase 14: Genetic Strategy Evolution ─────────────────
    evolution = StrategyEvolution(
        bus, population_size=50, elite_pct=0.2,
        mutation_rate=0.15, max_generations=100,
    )
    log.info("Strategy evolution: genetic algorithm (pop=%d, elite=%.0f%%)",
             evolution.population_size, evolution.elite_pct * 100)

    # ── Phase 15: Multi-Model AI Brain ───────────────────────
    ai_brain = AIBrain(
        bus, provider=os.getenv("AI_BRAIN_PROVIDER", "groq"),
        model=os.getenv("AI_BRAIN_MODEL", ""),
        api_key=os.getenv("AI_BRAIN_API_KEY", ""),
    )
    log.info("AI brain: %s/%s", ai_brain.provider, ai_brain.model)

    # ── Phase 16: Narrative Engine ───────────────────────────
    narrative = NarrativeEngine(bus, window_s=300.0, min_events_for_narrative=2)
    log.info("Narrative engine: event correlation -> market stories")

    # ── Phase 17: Whale Mirror Agent ─────────────────────────
    whale_mirror = WhaleMirrorAgent(
        bus, max_mirror_pct=float(os.getenv("MIRROR_MAX_PCT", "5")),
        min_whale_reliability=0.6,
        max_mirror_usd=float(os.getenv("MIRROR_MAX_USD", "2000")),
    )
    log.info("Whale mirror: auto-copy smart money (max=$%.0f, %.0f%% cap)",
             whale_mirror.max_mirror_usd, whale_mirror.max_mirror_pct)

    # ── Phase 18: Intent Solver Network ──────────────────────
    solver_net = IntentSolverNetwork(bus, auction_window_s=5.0)
    log.info("Intent solver: %d competing solvers for best execution",
             len(solver_net._solvers))

    # ── Phase 19: Liquidation Cascade Shield ─────────────────
    liq_shield = LiquidationShield(
        bus, auto_deleverage=True,
        deleverage_pct=float(os.getenv("SHIELD_DELEVERAGE_PCT", "25")),
    )
    log.info("Liquidation shield: auto-deleverage at health < %.1f",
             liq_shield.DANGER_THRESHOLD)

    # ── Phase 20: Agent Governance DAO ───────────────────────
    governance = GovernanceDAO(bus, elo_tracker=elo_tracker)
    log.info("Governance DAO: ELO-weighted voting, %.0f%% quorum, %.0f%% approval",
             30.0, 60.0)

    # ── Agent Registry (Unstoppable Domains identity) ─────────
    agent_registry = AgentRegistry(
        bus, owner_address=os.getenv("WALLET_ADDRESS", ""),
        chain_id=int(os.getenv("AGENT_REGISTRY_CHAIN", "8453")),
        ud_api_key=os.getenv("UD_API_KEY", ""),
    )
    agent_registry.register_internal_agents()
    log.info("Agent registry: %d agents registered on %s",
             len(agent_registry.agents), agent_registry._domain_base)

    # ── Private Key (used by hardware signer, x402, ERC-8004) ──
    private_key = os.getenv("PRIVATE_KEY", "")

    # ── Hardware Signing Pipeline (Maki/LeAgent pattern) ──────
    hw_signer = HardwareSigningPipeline(
        bus,
        auto_approve_usd=float(os.getenv("HARDWARE_APPROVE_USD", "500")),
        signer_mode=os.getenv("HARDWARE_SIGNER_MODE", "hot_wallet"),
        private_key=private_key,
    )
    log.info("Hardware signer: mode=%s auto_approve=$%.0f",
             hw_signer.signer_mode, hw_signer.auto_approve_usd)

    # ── x402 Payment Gateway (agent-to-agent commerce) ────────
    x402 = None
    if private_key:
        x402 = X402PaymentGateway(bus, private_key=private_key)
        await x402.start()
        log.info("x402 payment gateway: %d services, address=%s",
                 len(x402.ledger.services), x402._address or "sim-mode")

    # ── Hyperliquid DEX (perps data + execution) ────────────────
    hl_agent = HyperliquidAgent(bus, assets=assets, interval=15.0)
    supervisor.register("hyperliquid", hl_agent.run, stale_after=45.0, stoppable=hl_agent)
    if os.getenv("HYPERLIQUID_WALLET_KEY"):
        hl_executor = HyperliquidExecutor(bus)
        log.info("Hyperliquid: data + execution enabled")
    else:
        log.info("Hyperliquid: data-only mode (no wallet key)")

    # ── Backtester (RBI: Research → Backtest → Implement) ─────
    backtester = BacktesterAgent(
        bus, assets=assets, interval=3600.0,
        min_sharpe=0.5, min_win_rate=0.45, max_drawdown=25.0,
    )
    supervisor.register("backtester", backtester.run, stale_after=7200.0, stoppable=backtester)
    log.info("Backtester: 10 strategies × %d assets, walk-forward + Monte Carlo", len(assets))

    # ── Jupiter DEX (Solana execution) ────────────────────────
    sol_tokens = ["SOL", "JUP", "BONK", "WIF", "PYTH", "JTO", "RAY"]
    jup_scout = JupiterPriceScout(bus, tokens=sol_tokens, interval=10.0)
    supervisor.register("jupiter_prices", jup_scout.run, stale_after=30.0, stoppable=jup_scout)
    if os.getenv("SOLANA_PRIVATE_KEY"):
        jup_executor = JupiterExecutor(bus, wallet_address=os.getenv("SOLANA_WALLET_ADDRESS"))
        log.info("Jupiter: Solana execution enabled (%d tokens)", len(sol_tokens))
    else:
        log.info("Jupiter: price feeds only (no Solana key)")

    # ── BirdEye (Solana token analytics) ──────────────────────
    if os.getenv("BIRDEYE_API_KEY"):
        birdeye = BirdEyeAgent(bus, tokens=sol_tokens, interval=30.0)
        supervisor.register("birdeye", birdeye.run, stale_after=90.0, stoppable=birdeye)
        log.info("BirdEye: Solana analytics for %s", sol_tokens)

    # ── Social Media Agents (X/Discord/Telegram) ─────────────
    if os.getenv("X_BEARER_TOKEN"):
        x_monitor = XMonitorAgent(bus, assets=assets, interval=60.0)
        supervisor.register("x_monitor", x_monitor.run, stale_after=180.0, stoppable=x_monitor)
        log.info("X/Twitter monitor enabled for: %s", assets)
    discord_agent = DiscordAgent(bus)
    telegram_agent = TelegramAgent(bus)
    social_agg = SocialAggregator(bus, assets=assets)
    log.info("Social agents: Discord=%s Telegram=%s",
             "ON" if discord_agent._enabled else "OFF",
             "ON" if telegram_agent._enabled else "OFF")

    # ── Agent Learning Coordinator ────────────────────────────
    learning = LearningCoordinator(bus, analysis_interval=300.0)
    supervisor.register("learning", learning.run, stale_after=600.0, stoppable=learning)
    log.info("Learning coordinator: exploration=%.0f%%, analysis every 5min",
             learning.exploration_rate * 100)

    # ── Strategy ────────────────────────────────────────────────
    tokens = set(assets) | {"USD", "USDC", "USDT"}
    hermes = None
    if getattr(args, "hermes", False):
        hermes = HermesBrain(
            bus, portfolio=portfolio, assets=assets,
            base_size=args.base_size, max_size=args.max_size,
            capital=args.capital,
        )
        supervisor.register("hermes_brain", hermes.run, stale_after=60.0, stoppable=hermes)
        log.info("HERMES BRAIN enabled — LLM controls all agents (model=%s)", hermes.model)
        # Still create Strategist so its signal subscriptions populate the bus,
        # but Hermes will be the primary intent emitter
        strategist = Strategist(bus, base_size=args.base_size, portfolio=portfolio)
        log.info("Rule-based Strategist running as backup alongside Hermes")
    else:
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
    sor = SmartOrderRouter(bus, ws_client=ws_v2_client)
    supervisor.register("sor", sor.run, stale_after=15.0)
    log.info("Smart order router: 5 venues (Kraken, Binance, Coinbase, OKX, dYdX)%s",
             " + real L2 book" if ws_v2_client else "")

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
        RiskAgent(bus, "agent_policy", policy_check(policy_engine)),
    ]
    Coordinator(bus, n_risk_agents=len(risks))

    # ── Circuit Breakers ────────────────────────────────────────
    cb = CircuitBreaker(
        bus, kill_switch,
        max_consecutive_losses=5,
        max_drawdown_usd=args.max_drawdown,
        vol_halt_threshold=0.05,
        cooldown_seconds=300.0,
        ws_client=ws_v2_client,
    )
    if args.mode != "mock":
        PositionFlattener(bus, paper=(args.mode == "paper"),
                          kill_switch=kill_switch, ws_client=ws_v2_client)

    # ── Price Validation Gate (multi-source price check) ───────
    price_gate = PriceValidationGate(bus, safe_bps=50, warn_bps=200)
    log.info("Price gate: SAFE<%dbps WARN<%dbps HALT>=200bps", 50, 200)

    # ── Execution Pipeline ──────────────────────────────────────
    Simulator(bus, kill_switch=kill_switch)
    if args.mode == "mock":
        Executor(bus, kill_switch=kill_switch, dry_run=True, portfolio=portfolio)
    else:
        kraken_executor = KrakenExecutor(
            bus, kill_switch=kill_switch, paper=(args.mode == "paper"),
            portfolio=portfolio, client=kraken_client,
        )
        # Crash recovery: cancel orphaned orders + start dead man's switch
        await kraken_executor.start()
        log.info("KrakenExecutor started (paper=%s, dead_man_switch=%s)",
                 args.mode == "paper", args.mode == "live")

        # Order lifecycle tracker — polls for fill confirmation + stuck detection
        from .kraken import OrderTracker
        order_tracker = OrderTracker(bus, client=kraken_client, poll_interval=2.0)
        supervisor.register("order_tracker", order_tracker.run,
                            stale_after=10.0, stoppable=order_tracker)
        log.info("Order lifecycle tracker enabled (poll=2s)")

        # Exchange balance reconciliation — detects drift every 60s
        from .reconciliation import BalanceReconciler
        balance_recon = BalanceReconciler(
            bus, portfolio, wallet, interval=60.0,
            max_drift_usd=5.0, max_drift_pct=0.02,
        )
        supervisor.register("balance_reconciler", balance_recon.run,
                            stale_after=120.0, stoppable=balance_recon)
        log.info("Balance reconciler enabled (interval=60s, max_drift=$5/2%%)")

    Auditor(bus, db=db, state=state, portfolio=portfolio)

    # ── TWAP Execution (splits large orders) ───────────────────
    TWAPExecutor(bus, threshold=args.max_size * 0.75, n_slices=5, window_s=60.0)

    # ── Terminal Dashboard ──────────────────────────────────────
    if args.dashboard:
        dash = Dashboard(bus, state, refresh=1.0)
        supervisor.register("dashboard", dash.run, stale_after=5.0, stoppable=dash)

    # ── Agent Gateway ──────────────────────────────────────────
    # Auto-enable gateway when --web is used so it's accessible from the UI
    gateway = None
    if args.gateway or args.web or os.getenv("SWARM_GATEWAY"):
        master_key = args.gateway_key or os.getenv("SWARM_GATEWAY_KEY")
        gateway = AgentGateway(
            bus, strategist=strategist, portfolio=portfolio,
            master_key=master_key,
        )
        # Wire gateway to HermesBrain so it pushes briefs to connected agents
        if hermes:
            hermes.gateway = gateway
        log.info("Agent Gateway enabled — external agents can connect")
        log.info("  master_key configured (length=%d)", len(gateway.master_key))
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

    # ── ERC-8004 On-Chain Identity + Reputation ──────────────────
    erc8004 = None
    if args.erc8004 or private_key:
        if private_key:
            erc8004 = ERC8004Pipeline(
                bus, private_key=private_key,
                network=getattr(args, "erc8004_network", "sepolia"),
            )
            try:
                await erc8004.start()
                log.info("ERC-8004 ONLINE: %s on %s",
                         erc8004.status().get("address", "?"),
                         erc8004.status().get("network", "?"))
                log.info("  Agent ID: %s", erc8004.status().get("agent_id"))
                log.info("  Identity Registry: %s",
                         erc8004.status().get("identity_registry"))
            except Exception as e:
                log.warning("ERC-8004 startup failed (continuing without): %s", e)
                erc8004 = None
        else:
            log.warning("--erc8004 enabled but PRIVATE_KEY not set; skipping on-chain integration")

    # ── Uniswap DEX Execution (Base) ──────────────────────────────
    uniswap = None
    uniswap_api_key = os.getenv("UNISWAP_API_KEY", "")
    if private_key and uniswap_api_key:
        uniswap = UniswapExecutor(
            bus, private_key=private_key,
            api_key=uniswap_api_key,
            chain_id=int(os.getenv("UNISWAP_CHAIN_ID", "8453")),
        )
        # Add Uniswap as a real venue in the SOR
        from .sor import VenueConfig as _VC
        sor.venues.append(_VC(
            name="uniswap_base",
            base_url="https://trade-api.gateway.uniswap.org/v1",
            fee_rate=0.003,
            weight=0.95,
        ))
        log.info("Uniswap DEX executor ONLINE: chain=%d address=%s",
                 uniswap._chain_id, uniswap._address)
    elif uniswap_api_key:
        log.info("Uniswap API key set but no PRIVATE_KEY — DEX execution disabled")

    # ── Web Dashboard ──────────────────────────────────────────
    if args.web:
        # Capital allocator for agent leaderboard
        from .capital_allocator import CapitalAllocator
        cap_alloc = CapitalAllocator(bus, total_capital=args.capital)

        # Social trading engine
        from .social_trading import SocialTradingEngine
        social = SocialTradingEngine(bus, db=db)

        # Agent social network (posts, communities, data feeds, DMs)
        from .swarm_network import SwarmNetwork
        network = SwarmNetwork(bus)

        web_dash = WebDashboard(
            bus, state, db=db,
            kill_switch=kill_switch, host=args.web_host, port=args.web_port,
            wallet=wallet, gateway=gateway,
            strategist=strategist, capital_allocator=cap_alloc,
            memory=memory, erc8004=erc8004, uniswap=uniswap,
            social=social, network=network,
        )
        await web_dash.start()
        log.info("Web dashboard: http://localhost:%d", args.web_port)

        # Auto-register built-in agents with social profiles
        agent_profiles = [
            ("momentum", "Momentum", ["momentum", "trend"], "Rate-of-change momentum over 20-period window"),
            ("mean_rev", "Mean Reversion", ["mean-reversion", "z-score"], "Z-score reversion detection over 50 periods"),
            ("vol", "Volatility", ["volatility", "regime"], "Volatility regime detection and sizing"),
            ("rsi", "RSI Agent", ["technical", "oscillator"], "Relative Strength Index signals"),
            ("macd", "MACD Agent", ["technical", "crossover"], "MACD crossover detection"),
            ("bollinger", "Bollinger Agent", ["technical", "squeeze"], "Bollinger Band squeeze and breakout"),
            ("ichimoku", "Ichimoku Agent", ["technical", "cloud"], "Ichimoku Cloud trend analysis"),
            ("ml", "ML Signal", ["machine-learning", "adaptive"], "Online gradient-boosted decision trees"),
            ("whale", "Whale Tracker", ["on-chain", "whale"], "Large BTC/ETH transaction monitoring"),
            ("news", "News Sentiment", ["sentiment", "news"], "CryptoPanic news analysis"),
            ("confluence", "Confluence", ["meta", "multi-signal"], "Multi-group signal agreement scoring"),
        ]
        for aid, name, tags, desc in agent_profiles:
            social.create_profile(aid, name, strategy_tags=tags, strategy_description=desc)
        log.info("Social trading: %d agent profiles registered", len(agent_profiles))

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

    # ── Checkpoint — periodic state saving ───────────────────────
    if ckpt:
        ckpt.strategist = strategist
        supervisor.register("checkpoint", ckpt.run, stale_after=60.0)
        log.info("Checkpoint enabled: saving every %.0fs to %s",
                 ckpt.interval, ckpt.path)

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

    # ── SIGTERM/SIGINT handler for graceful shutdown ─────────────
    import signal
    shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown_event.set)

    # ── Run ──────────────────────────────────────────────────────
    try:
        # Wait for either duration or shutdown signal
        sleep_task = asyncio.create_task(asyncio.sleep(args.duration))
        signal_task = asyncio.create_task(shutdown_event.wait())
        done, pending = await asyncio.wait(
            {sleep_task, signal_task}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if shutdown_event.is_set():
            log.info("Received shutdown signal")
    except asyncio.CancelledError:
        pass
    finally:
        log.info("SWARM SHUTDOWN starting...")

        # Cancel all open orders on exchange before shutting down agents
        if args.mode in ("paper", "live"):
            try:
                from .kraken import _run_cli
                log.info("Cancelling all open orders on Kraken...")
                await _run_cli("cancel", "--all", timeout=10.0)
                log.info("All open orders cancelled")
            except Exception as e:
                log.warning("Failed to cancel orders on shutdown: %s", e)

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
        # Close web dashboard (aiohttp server + WebSocket connections)
        if 'web_dash' in locals():
            try:
                await web_dash.stop()
                log.info("Web dashboard stopped")
            except Exception as e:
                log.warning("Web dashboard stop failed: %s", e)

        # Close Kraken WebSocket client
        if ws_v2_client is not None:
            try:
                await ws_v2_client.close()
                log.info("Kraken WS client closed")
            except Exception as e:
                log.warning("Kraken WS close failed: %s", e)

        # Close Kraken REST client session
        try:
            from .kraken_api import close_client
            await close_client()
            log.info("Kraken REST client closed")
        except Exception as e:
            log.debug("Kraken REST close: %s", e)

        # Close database connection pool
        await db.close()
        log.info("SWARM SHUTDOWN complete")

    # ── Write session memory ──────────────────────────────────────
    try:
        memory.write_session_notes()
        log.info("Session memory persisted to strategy_notes.md")
    except Exception as e:
        log.warning("Failed to write session notes: %s", e)

    log.info("FINAL daily_pnl=%+.4f trades=%d equity=%.4f fees=%.4f",
             state["daily_pnl"], state.get("trade_count", 0),
             portfolio.total_equity(), portfolio.total_fees())
    import json as _json
    log.info("POSITIONS %s", _json.dumps(pos_mgr.summary(), indent=2))
    log.info("WALLET %s", _json.dumps(wallet.summary(), indent=2))
    if erc8004:
        log.info("ERC-8004 %s", _json.dumps(erc8004.status(), indent=2))
    if uniswap:
        log.info("UNISWAP %s", _json.dumps(uniswap.status(), indent=2))
    log.info("SMART_MONEY %s", _json.dumps(smart_money.summary(), indent=2))
    log.info("YIELD %s", _json.dumps(yield_agg.summary(), indent=2))
    log.info("LP_MANAGER %s", _json.dumps(lp_mgr.summary(), indent=2))
    log.info("POLICIES %s", _json.dumps(policy_engine.summary(), indent=2))
    log.info("REGISTRY %s", _json.dumps(agent_registry.summary(), indent=2))
    log.info("SIGNER %s", _json.dumps(hw_signer.status(), indent=2))
    if x402:
        log.info("X402 %s", _json.dumps(x402.status(), indent=2))
    log.info("ALPHA_SWARM %s", _json.dumps(alpha_components["coordinator"].summary(), indent=2))
    log.info("POLYMARKET %s", _json.dumps(polymarket.summary(), indent=2))

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
        except Exception as e:
            log.debug("Paper account status check failed: %s", e)


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(run(args))
