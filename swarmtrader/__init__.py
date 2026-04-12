from .core import Bus, MarketSnapshot, Signal, TradeIntent, RiskVerdict, ExecutionReport, OrderSpec
from .agents import MockScout, MomentumAnalyst, MeanReversionAnalyst, VolatilityAnalyst
from .strategy import Strategist, RiskAgent, Coordinator, size_check, allowlist_check, drawdown_check
from .execution import Simulator, Executor, Auditor
from .kraken import KrakenScout, KrakenWSScout, KrakenExecutor, OrderTracker
from .signals import PRISMSignalAgent
from .agents_advanced import OrderBookAgent, FundingRateAgent, SpreadAgent, RegimeAgent
from .safety import KillSwitch, CircuitBreaker, PositionFlattener
from .risk import (
    RateLimiter, DailyDrawdownTracker, rate_limit_check,
    ConcentrationLimiter, GrossExposureLimiter,
    concentration_check, gross_exposure_check, min_order_check, daily_drawdown_check,
)
from .dashboard import Dashboard
from .strategies import (
    RSIAgent, MACDAgent, BollingerAgent, VWAPAgent, IchimokuAgent,
    LiquidationCascadeAgent, ATRTrailingStopAgent, depth_liquidity_check,
)
from .news import NewsAgent
from .multitf import MultiTimeframeMomentum
from .correlation import CorrelationAgent
from .positions import PositionManager, max_positions_check
from .twap import TWAPExecutor
from .confluence import ConfluenceDetector
from .whale import WhaleAgent
from .open_interest import OpenInterestAgent
from .feargreed import FearGreedAgent
from .social import SocialSentimentAgent
from .liquidation import LiquidationAgent
from .onchain import OnChainAgent
from .arbitrage import ArbitrageAgent
from .wallet import WalletManager, funds_check, allocation_check
from .automation import AgentSupervisor, Scheduler, build_scheduler, HeartbeatMixin
from .portfolio_opt import (
    CovarianceEstimator, MarkowitzOptimizer, RiskParityOptimizer,
    BlackLittermanModel, DynamicHedger, PortfolioOptAgent, rebalance_check,
)
# Quantitative risk
from .var import VaREngine, var_check
from .stress_test import StressTester, StressScenario, HISTORICAL_SCENARIOS, stress_check
# Smart order routing + microstructure
from .sor import SmartOrderRouter, ExchangeQuote, VenueConfig, sor_venue_check
from .microstructure import (
    MarketImpactModel, AlmgrenChrissModel, IcebergExecutor, ExecutionQualityTracker,
)
# ML signal
from .ml_signal import MLSignalAgent, FeatureEngine, GradientBooster, ml_model_check
# Factor model + PnL attribution
from .factor_model import FactorModel, PnLAttributor, FactorRiskModel, factor_exposure_check
# Compliance + data quality
from .compliance import (
    WashTradingDetector, PositionLimitChecker, MarginMonitor,
    Reconciler, DataQualityMonitor, compliance_check,
)
# Walk-forward + TCA
from .walkforward import (
    WalkForwardEngine, WalkForwardResult, TransactionCostAnalyzer, MonteCarloBacktester,
)
# Agent gateway
from .gateway import AgentGateway
# Agent social network
from .swarm_network import SwarmNetwork
# Migrations + metrics
from .migrations import run_migrations, get_migration_status
from .metrics import MetricsCollector
# Agent memory
from .memory import AgentMemory
# ERC-8004 on-chain identity + reputation
from .erc8004 import ERC8004Agent, TradeIntentSigner, ERC8004Pipeline
# Uniswap DEX execution on Base
from .uniswap import UniswapExecutor, UniswapClient, uniswap_venue_config
# Phase 4: config, reconciliation, structured logging
from .config import TradingConfig
from .reconciliation import BalanceReconciler
from .logging_config import setup_logging
# Phase 5: checkpoint, rate limiting, demo
from .checkpoint import Checkpoint
from .rate_limit import APIRateLimiter, get_api_limiter
from .demo import DemoScout, MultiAssetDemoScout
# Extended data feeds
from .feeds import (
    ExchangeFlowAgent, StablecoinAgent, MacroCalendarAgent,
    DeribitOptionsAgent, TokenUnlockAgent, GitHubDevAgent, RSSNewsAgent,
)
# Native Kraken API + WS v2
from .kraken_api import KrakenRESTClient, KrakenRateLimiter, KrakenAPIConfig
from .kraken_ws import KrakenWSv2Client, OrderBookSnapshot
# Competition features
from .nlp_strategy import parse_strategy, apply_strategy, PRESETS as STRATEGY_PRESETS
from .pyth_oracle import PythOracle
from .dex_quotes import DEXQuoteProvider
from .strategy_privacy import StrategyPrivacyManager
from .capital_allocator import CapitalAllocator
# ETHGlobal-inspired agentic trading modules
from .smart_money import SmartMoneyAgent, TrackedWallet, SmartMoneyFlow
from .x402_payments import X402PaymentGateway, PaymentLedger, PricingTier
from .lp_manager import LPRebalanceManager, LPPosition
from .yield_aggregator import YieldAggregator, YieldPool, VaultPosition
from .agent_policies import AgentPolicyEngine, AgentPolicy, policy_check
from .hardware_signer import HardwareSigningPipeline, HotWalletSigner, LedgerSigner
from .polymarket import PolymarketAgent
from .alpha_swarm import (
    AlphaHunter, SentimentFilter, RiskScreener, SwarmCoordinator,
    setup_alpha_swarm, AlphaOpportunity,
)
# Competitive intelligence modules (ETHGlobal showcase patterns)
from .price_gate import PriceValidationGate
from .kalman import AdaptiveKalmanFilter
from .fusion import DataFusionPipeline, FusedSignal
from .debate import (
    AdversarialDebateEngine, BullAgent, BearAgent, DebateResolver,
    ELOTracker, setup_debate_engine,
)
from .agent_policies import ExecutionSandbox
# Phase 4-10: ETHGlobal showcase integrations
from .vault import VaultManager, vault_trade_check
from .marketplace import AgentMarketplace
from .flashloan import FlashLoanExecutor, ArbPathFinder, ProfitSimulator
from .strategy_nft import StrategyNFTManager, StrategyConfig, StrategyNFT
from .v4_hooks import V4HookManager, V4PoolState, V4Position
from .cross_chain import CrossChainCoordinator
from .zk_trading import CommitRevealEngine, PrivatePositionManager
# Phase 11-20
from .rugpull_detector import RugpullDetector, rugpull_check
from .prediction_trader import PredictionTrader
from .agent_payments import AgentPaymentProtocol
from .strategy_evolution import StrategyEvolution, StrategyGenome
from .ai_brain import AIBrain
from .narrative import NarrativeEngine
from .whale_mirror import WhaleMirrorAgent
from .intent_solver import IntentSolverNetwork, Solver
from .liquidation_shield import LiquidationShield
from .governance import GovernanceDAO
# Phase 21-30
from .telegram_bot import TelegramTradingBot
from .voice_trading import VoiceTradingEngine
from .agent_factory import AgentFactory
from .options_engine import OptionsEngine
from .social_alpha_v2 import SocialAlphaScanner
from .backtesting_arena import BacktestingArena
from .observability import ObservabilityDashboard
from .federated import FederatedCoordinator
from .rwa_bridge import RWABridge
from .treasury import AutonomousTreasury
# Phase 31-40
from .mev_engine import MEVEngine
from .agent_memory_v2 import AgentMemoryDAG
from .sentiment_derivatives import SentimentDerivativesEngine
from .grid_trading import GridTradingEngine
from .token_sniper import TokenSniper
from .regime_v2 import CorrelationRegimeDetector
from .gas_optimizer import GasOptimizer
from .portfolio_insurance import PortfolioInsurance
from .agent_protocol import AgentCommunicationProtocol
from .swarm_consensus import SwarmConsensus
from .agent_registry import AgentRegistry, AgentIdentity
# Multi-exchange + arbitrage
from .exchanges import (
    ExchangeClient, BinanceClient, CoinbaseClient, OKXClient, BybitClient,
    get_exchange, list_exchanges, get_all_tickers, Ticker, OrderResult, Balance,
)
from .arb_executor import ArbScanner, ArbExecutor
# Moon Dev tech — Hyperliquid, Jupiter, BirdEye, Backtester, Social, Learning
from .hyperliquid import (
    HyperliquidAgent, HyperliquidExecutor, HyperliquidClient,
    HyperliquidWSClient, hyperliquid_venue_config,
)
from .backtester import (
    BacktesterAgent, BacktestEngine, HistoricalDataFetcher,
    BacktestResult, STRATEGIES as BACKTEST_STRATEGIES,
    MomentumStrategy, MeanReversionStrategy, RSIMomentumStrategy,
    DCAStrategy, BreakoutStrategy,
)
from .jupiter import (
    JupiterExecutor, JupiterClient, JupiterPriceScout, jupiter_venue_config,
)
from .birdeye import BirdEyeAgent, BirdEyeClient
from .social_agents import (
    XMonitorAgent, DiscordAgent, TelegramAgent, SocialAggregator,
)
from .agent_learning import LearningCoordinator
from .hermes_brain import HermesBrain, CommanderGate
from .dex_multi import (
    MultiDEXScanner, SushiSwapQuoter, AerodromeQuoter, CurveQuoter,
    PancakeSwapQuoter, RaydiumQuoter, OrcaQuoter, DEXQuote,
)

__all__ = [
    "Bus", "MarketSnapshot", "Signal", "TradeIntent", "RiskVerdict", "ExecutionReport",
    "MockScout", "MomentumAnalyst", "MeanReversionAnalyst", "VolatilityAnalyst",
    "Strategist", "RiskAgent", "Coordinator",
    "size_check", "allowlist_check", "drawdown_check",
    "Simulator", "Executor", "Auditor",
    "KrakenScout", "KrakenWSScout", "KrakenExecutor",
    "PRISMSignalAgent",
    "OrderBookAgent", "FundingRateAgent", "SpreadAgent", "RegimeAgent",
    "KillSwitch", "CircuitBreaker", "PositionFlattener",
    "RateLimiter", "DailyDrawdownTracker", "rate_limit_check",
    "ConcentrationLimiter", "GrossExposureLimiter",
    "concentration_check", "gross_exposure_check", "min_order_check", "daily_drawdown_check",
    "Dashboard",
    # TA strategy agents
    "RSIAgent", "MACDAgent", "BollingerAgent", "VWAPAgent", "IchimokuAgent",
    "LiquidationCascadeAgent", "ATRTrailingStopAgent", "depth_liquidity_check",
    "NewsAgent",
    "MultiTimeframeMomentum", "CorrelationAgent",
    "PositionManager", "max_positions_check",
    "TWAPExecutor", "ConfluenceDetector", "WhaleAgent",
    # Market intelligence agents
    "OpenInterestAgent", "FearGreedAgent", "SocialSentimentAgent",
    "LiquidationAgent", "OnChainAgent", "ArbitrageAgent",
    # Wallet management
    "WalletManager", "funds_check", "allocation_check",
    # Automation
    "AgentSupervisor", "Scheduler", "build_scheduler", "HeartbeatMixin",
    # Portfolio optimization
    "CovarianceEstimator", "MarkowitzOptimizer", "RiskParityOptimizer",
    "BlackLittermanModel", "DynamicHedger", "PortfolioOptAgent", "rebalance_check",
    # Quantitative risk
    "VaREngine", "var_check",
    "StressTester", "StressScenario", "HISTORICAL_SCENARIOS", "stress_check",
    # Smart order routing + microstructure
    "SmartOrderRouter", "ExchangeQuote", "VenueConfig", "sor_venue_check",
    "MarketImpactModel", "AlmgrenChrissModel", "IcebergExecutor", "ExecutionQualityTracker",
    # ML signal
    "MLSignalAgent", "FeatureEngine", "GradientBooster", "ml_model_check",
    # Factor model + PnL attribution
    "FactorModel", "PnLAttributor", "FactorRiskModel", "factor_exposure_check",
    # Compliance + data quality
    "WashTradingDetector", "PositionLimitChecker", "MarginMonitor",
    "Reconciler", "DataQualityMonitor", "compliance_check",
    # Walk-forward + TCA
    "WalkForwardEngine", "WalkForwardResult", "TransactionCostAnalyzer", "MonteCarloBacktester",
    # Agent gateway + social network
    "AgentGateway",
    "SwarmNetwork",
    # Config, reconciliation, logging
    "TradingConfig", "BalanceReconciler", "setup_logging",
    # Checkpoint, rate limiting, demo
    "Checkpoint", "APIRateLimiter", "get_api_limiter",
    "DemoScout", "MultiAssetDemoScout",
    # ERC-8004 on-chain identity + reputation
    "ERC8004Agent", "TradeIntentSigner", "ERC8004Pipeline",
    # Uniswap DEX execution
    "UniswapExecutor", "UniswapClient", "uniswap_venue_config",
    # Extended data feeds
    "ExchangeFlowAgent", "StablecoinAgent", "MacroCalendarAgent",
    "DeribitOptionsAgent", "TokenUnlockAgent", "GitHubDevAgent", "RSSNewsAgent",
    # Native Kraken API + WS v2
    "KrakenRESTClient", "KrakenRateLimiter", "KrakenAPIConfig",
    "KrakenWSv2Client", "OrderBookSnapshot", "OrderTracker", "OrderSpec",
    # Competition features
    "parse_strategy", "apply_strategy", "STRATEGY_PRESETS",
    "PythOracle", "DEXQuoteProvider", "StrategyPrivacyManager", "CapitalAllocator",
    # ETHGlobal-inspired agentic trading modules
    "SmartMoneyAgent", "TrackedWallet", "SmartMoneyFlow",
    "X402PaymentGateway", "PaymentLedger", "PricingTier",
    "LPRebalanceManager", "LPPosition",
    "YieldAggregator", "YieldPool", "VaultPosition",
    "AgentPolicyEngine", "AgentPolicy", "policy_check",
    "HardwareSigningPipeline", "HotWalletSigner", "LedgerSigner",
    "PolymarketAgent",
    "AlphaHunter", "SentimentFilter", "RiskScreener", "SwarmCoordinator",
    "setup_alpha_swarm", "AlphaOpportunity",
    "AgentRegistry", "AgentIdentity",
    # Moon Dev tech
    "HyperliquidAgent", "HyperliquidExecutor", "HyperliquidClient",
    "HyperliquidWSClient", "hyperliquid_venue_config",
    "BacktesterAgent", "BacktestEngine", "HistoricalDataFetcher",
    "BacktestResult", "BACKTEST_STRATEGIES",
    "MomentumStrategy", "MeanReversionStrategy", "RSIMomentumStrategy",
    "DCAStrategy", "BreakoutStrategy",
    "JupiterExecutor", "JupiterClient", "JupiterPriceScout", "jupiter_venue_config",
    "BirdEyeAgent", "BirdEyeClient",
    "XMonitorAgent", "DiscordAgent", "TelegramAgent", "SocialAggregator",
    "LearningCoordinator",
    # Hermes LLM brain
    "HermesBrain",
]
