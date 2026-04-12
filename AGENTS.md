# SWARM TRADE — Agent Reference

80+ cooperative AI agents organized by function. All communicate via the async event `Bus` using pub/sub signals.

## Data Scouts

| Agent | Source | API Key Required | Interval |
|-------|--------|-----------------|----------|
| `MockScout` | Simulated GBM prices | No | 0.2s |
| `KrakenScout` | Kraken REST API v2 | No (public) | 2s |
| `KrakenWSScout` | Kraken WebSocket v2 (book + trades + executions) | No (public) | Stream |
| `DemoScout` | Pre-recorded replay | No | 0.3s |
| `HyperliquidAgent` | Hyperliquid L2 + funding + OI | No (public) | 15s |
| `JupiterPriceScout` | Jupiter DEX aggregated quotes (Solana) | No | 10s |
| `BirdEyeAgent` | BirdEye Solana token analytics | `BIRDEYE_API_KEY` | 30s |
| `PythOracle` | Pyth Network decentralized price feeds | No | 10s |

## Core Analysts

| Agent | Strategy | Signal |
|-------|----------|--------|
| `MomentumAnalyst` | Price momentum (rate of change) | `signal.momentum` |
| `MeanReversionAnalyst` | Z-score reversion to mean | `signal.mean_reversion` |
| `VolatilityAnalyst` | Volatility regime detection | `signal.volatility` |

## Technical Analysis

| Agent | Indicator | Signal |
|-------|-----------|--------|
| `RSIAgent` | Relative Strength Index (7-period, 75/25) | `signal.rsi` |
| `MACDAgent` | MACD crossover (8/21/5 fast crypto) | `signal.macd` |
| `BollingerAgent` | Bollinger Band squeeze/breakout (2.5 std) | `signal.bollinger` |
| `VWAPAgent` | Volume-Weighted Average Price (120-tick) | `signal.vwap` |
| `IchimokuAgent` | Ichimoku Cloud trend analysis | `signal.ichimoku` |
| `LiquidationCascadeAgent` | Liquidation cascade detection | `signal.liq_cascade` |
| `ATRTrailingStopAgent` | ATR-based dynamic stops | `signal.atr_stop` |

## Advanced Market

| Agent | Intelligence | Signal |
|-------|-------------|--------|
| `OrderBookAgent` | Order book L2 depth imbalance | `signal.orderbook` |
| `FundingRateAgent` | Perpetual funding rates | `signal.funding` |
| `SpreadAgent` | Bid-ask spread analysis | `signal.spread` |
| `RegimeAgent` | Market regime classification (ADX + Hurst) | `signal.regime` |

## Market Intelligence

| Agent | Source | API Key Required |
|-------|--------|-----------------|
| `WhaleAgent` | Large BTC/ETH transaction tracking | No |
| `SmartMoneyAgent` | 25+ whale wallet shadowing (Etherscan/Basescan) | `ETHERSCAN_API_KEY` / `BASESCAN_API_KEY` |
| `OnChainAgent` | Etherscan on-chain activity | `ETHERSCAN_API_KEY` |
| `FearGreedAgent` | Alternative.me Fear & Greed Index | No |
| `SocialSentimentAgent` | Social media sentiment aggregation | No |
| `LiquidationAgent` | Futures liquidation levels | No |
| `OpenInterestAgent` | Futures open interest tracking | No |
| `ArbitrageAgent` | Cross-exchange price differences (CoinGecko) | No |
| `PolymarketAgent` | Polymarket prediction market CLOB signals | No |

## External Signals

| Agent | Source | API Key Required |
|-------|--------|-----------------|
| `NewsAgent` | CryptoPanic news sentiment | `NEWS_API_KEY` |
| `PRISMSignalAgent` | PRISM/Strykr AI signals | `PRISM_API_KEY` |

## Extended Data Feeds (feeds.py)

| Agent | Intelligence | API Key | Interval |
|-------|-------------|---------|----------|
| `ExchangeFlowAgent` | Exchange reserve inflow/outflow (CoinGecko) | `COINGECKO_API_KEY` (optional) | 5 min |
| `StablecoinAgent` | USDT/USDC market cap + depeg monitoring | `COINGECKO_API_KEY` (optional) | 10 min |
| `MacroCalendarAgent` | Economic events (FOMC, CPI, NFP) | `FRED_API_KEY` (optional) | 30 min |
| `DeribitOptionsAgent` | Options IV, put/call ratio, max pain | No (Deribit public) | 5 min |
| `TokenUnlockAgent` | Token vesting unlock schedule (DeFi Llama) | No | 60 min |
| `GitHubDevAgent` | Protocol commit velocity + releases | `GITHUB_TOKEN` (optional) | 30 min |
| `RSSNewsAgent` | Multi-source RSS (CoinDesk, CoinTelegraph, Decrypt) | No | 5 min |

## Cross-Asset Intelligence

| Agent | Function | Signal |
|-------|----------|--------|
| `MultiTimeframeMomentum` | Multi-timeframe trend alignment | `signal.mtf` |
| `CorrelationAgent` | Cross-asset correlation + lead-lag | `signal.correlation` |
| `ConfluenceDetector` | Multi-group signal agreement scoring | `signal.confluence` |

## Alpha Swarm Pipeline

| Agent | Role | Signal |
|-------|------|--------|
| `AlphaHunter` | Scans for breakout/dip/accumulation patterns | `alpha.opportunity` |
| `SentimentFilter` | Validates with social/news/on-chain data | `alpha.filtered` |
| `RiskScreener` | Applies risk limits + position sizing | `alpha.screened` |
| `SwarmCoordinator` | Final decision with minimum conviction threshold | `alpha.execute` |

## Social Media Agents

| Agent | Platform | API Key Required |
|-------|----------|-----------------|
| `XMonitorAgent` | X/Twitter real-time crypto alpha monitoring | `X_BEARER_TOKEN` |
| `DiscordAgent` | Discord webhook notifications | `DISCORD_WEBHOOK_URL` |
| `TelegramAgent` | Telegram bot alerts | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` |
| `SocialAggregator` | Cross-platform sentiment fusion | — (aggregates above) |

## Machine Learning

| Agent | Method | Signal |
|-------|--------|--------|
| `MLSignalAgent` | Online gradient-boosted decision trees (pure stdlib) | `signal.ml` |

## Strategy + Coordination

| Component | Role |
|-----------|------|
| `Strategist` | Regime-aware adaptive signal weighting, Kelly criterion sizing |
| `HermesBrain` | Local LLM (Ollama) replaces rule-based strategy with AI brain |
| `RiskAgent` | Quorum risk consensus from all risk layers |
| `Coordinator` | Converts approved intents into execution orders |
| `CapitalAllocator` | Agent leaderboard-based capital allocation |

## Risk Pipeline (15 layers)

| Layer | Check | Description |
|-------|-------|-------------|
| 1 | `size_check` | Position sizing limits |
| 2 | `allowlist_check` | Asset allowlist validation |
| 3 | `drawdown_check` | Daily drawdown limits |
| 4 | `rate_limit_check` | Trade frequency limits |
| 5 | `max_positions_check` | Open position count limit |
| 6 | `funds_check` | Sufficient funds verification |
| 7 | `allocation_check` | Per-asset allocation cap |
| 8 | `depth_liquidity_check` | Order book depth verification |
| 9 | `var_check` | Value-at-Risk (historical, parametric, Monte Carlo) |
| 10 | `stress_check` | 8 historical stress scenarios |
| 11 | `compliance_check` | Wash trade + margin + data quality |
| 12 | `factor_exposure_check` | Factor model exposure limits |
| 13 | `rebalance_check` | Portfolio rebalance triggers |
| 14 | `sor_venue_check` | Smart order routing venue selection |
| 15 | `agent_policy_check` | Per-agent spending cap enforcement |

## Execution

| Component | Function |
|-----------|----------|
| `Simulator` | Dry-run executor with slippage model |
| `Executor` | Generic order executor |
| `KrakenExecutor` | Kraken-specific live execution (REST v2 + CLI + dead man's switch) |
| `HyperliquidExecutor` | Hyperliquid perps execution (EVM wallet) |
| `JupiterExecutor` | Jupiter DEX execution on Solana |
| `UniswapExecutor` | Uniswap Trading API (DEX on Base) |
| `SmartOrderRouter` | Multi-venue quote comparison (5 CEX + 10 DEX) |
| `TWAPExecutor` | Time-weighted average price slicing |
| `IcebergExecutor` | Hidden large order execution |
| `ArbScanner` | Cross-venue price discrepancy detection (10s polls) |
| `ArbExecutor` | Simultaneous buy-cheap / sell-expensive execution |
| `ExecutionQualityTracker` | Transaction cost analysis |

## DEX Quoters

| Quoter | Chain(s) | Protocol |
|--------|----------|----------|
| `DEXQuoteProvider` | Multiple | 1inch + Jupiter aggregation |
| `SushiSwapQuoter` | Ethereum, Arbitrum, Base | SushiSwap v2/v3 |
| `AerodromeQuoter` | Base | Aerodrome (Velodrome fork) |
| `CurveQuoter` | Ethereum, Arbitrum | Curve stable swaps |
| `PancakeSwapQuoter` | BSC, Ethereum | PancakeSwap v3 |
| `RaydiumQuoter` | Solana | Raydium AMM |
| `OrcaQuoter` | Solana | Orca Whirlpools |

## Safety Systems

| Component | Function |
|-----------|----------|
| `KillSwitch` | Emergency halt all trading (file touch or button) |
| `CircuitBreaker` | Auto-halt on drawdown/volatility spike/consecutive losses |
| `PositionFlattener` | Force-close all positions on circuit breaker |
| `PositionManager` | Trailing/hard stop management + max hold time |
| `Reconciler` | Balance reconciliation vs exchange (60s interval) |
| `HardwareSigningPipeline` | Hot wallet / Ledger USB / Secure Enclave signing |
| `AgentPolicyEngine` | Per-agent spending limits + sandboxing |

## DeFi / On-Chain

| Component | Function |
|-----------|----------|
| `ERC8004Pipeline` | On-chain agent identity, reputation, EIP-712 intent signing |
| `X402PaymentGateway` | Agent-to-agent USDC micropayments |
| `LPRebalanceManager` | Uniswap v3 LP range rebalancing (UniRange pattern) |
| `YieldAggregator` | DeFi Llama yield discovery + auto-harvest |
| `AgentRegistry` | Unstoppable Domains agent identity on Base chain |
| `StrategyPrivacyManager` | Encrypted strategy storage |

## Learning + Backtesting

| Component | Function |
|-----------|----------|
| `AgentMemory` | Cross-session learning (SOUL.md + strategy_notes.md) |
| `LearningCoordinator` | Online performance tracking, exploration/exploitation |
| `BacktesterAgent` | 10-strategy walk-forward + Monte Carlo backtesting |
| `WalkForwardEngine` | Walk-forward backtesting framework |
| `MonteCarloBacktester` | Monte Carlo simulation for strategy validation |

## Infrastructure

| Component | Function |
|-----------|----------|
| `AgentSupervisor` | Health monitoring, auto-restart, heartbeats |
| `Checkpoint` | State checkpointing for crash recovery |
| `Database` | Postgres (Neon) + SQLite dual-mode with connection pooling |
| `Auditor` | SQLite/Postgres trade audit trail |
| `DataQualityMonitor` | Feed freshness and consistency checks |
| `TransactionCostAnalyzer` | Post-trade execution quality metrics |
| `WebDashboard` | Browser UI with WebSocket streaming + social trading |
| `Dashboard` | Terminal TUI dashboard |
| `AgentGateway` | HTTP/WebSocket bridge for external AI agents |
| `SocialTradingEngine` | Copy trading, leaderboard, strategy marketplace |
