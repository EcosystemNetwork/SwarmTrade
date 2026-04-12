# SWARM TRADE — Agent Reference

50+ cooperative AI agents organized by function. All communicate via the async event `Bus` using pub/sub signals.

## Data Scouts

| Agent | Source | API Key Required |
|-------|--------|-----------------|
| `MockScout` | Simulated GBM prices | No |
| `KrakenScout` | Kraken REST API | No (public) |
| `KrakenWSScout` | Kraken WebSocket v2 | No (public) |
| `DemoScout` | Pre-recorded replay | No |

## Core Analysts

| Agent | Strategy | Signal |
|-------|----------|--------|
| `MomentumAnalyst` | Price momentum (rate of change) | `signal.momentum` |
| `MeanReversionAnalyst` | Z-score reversion to mean | `signal.mean_reversion` |
| `VolatilityAnalyst` | Volatility regime detection | `signal.volatility` |

## Technical Analysis

| Agent | Indicator | Signal |
|-------|-----------|--------|
| `RSIAgent` | Relative Strength Index | `signal.rsi` |
| `MACDAgent` | MACD crossover | `signal.macd` |
| `BollingerAgent` | Bollinger Band squeeze/breakout | `signal.bollinger` |
| `VWAPAgent` | Volume-Weighted Average Price | `signal.vwap` |
| `IchimokuAgent` | Ichimoku Cloud | `signal.ichimoku` |
| `LiquidationCascadeAgent` | Liquidation cascade detection | `signal.liq_cascade` |
| `ATRTrailingStopAgent` | ATR-based dynamic stops | `signal.atr_stop` |

## Advanced Market

| Agent | Intelligence | Signal |
|-------|-------------|--------|
| `OrderBookAgent` | Order book imbalance | `signal.orderbook` |
| `FundingRateAgent` | Perpetual funding rates | `signal.funding` |
| `SpreadAgent` | Bid-ask spread analysis | `signal.spread` |
| `RegimeAgent` | Market regime classification | `signal.regime` |

## Market Intelligence

| Agent | Source | API Key Required |
|-------|--------|-----------------|
| `WhaleAgent` | Large transaction tracking | No |
| `OnChainAgent` | Etherscan on-chain activity | `ETHERSCAN_API_KEY` |
| `FearGreedAgent` | Alternative.me Fear & Greed | No |
| `SocialSentimentAgent` | Social media sentiment | No |
| `LiquidationAgent` | Futures liquidation levels | No |
| `ArbitrageAgent` | Cross-exchange price diffs | No |

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

## Machine Learning

| Agent | Method | Signal |
|-------|--------|--------|
| `MLSignalAgent` | Online gradient-boosted decision trees (pure stdlib) | `signal.ml` |

## Strategy + Coordination

| Component | Role |
|-----------|------|
| `Strategist` | Regime-aware adaptive signal weighting, Kelly criterion sizing |
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
| 15 | `ml_model_check` | ML model confidence threshold |

## Execution

| Component | Function |
|-----------|----------|
| `Simulator` | Dry-run executor with slippage model |
| `Executor` | Generic order executor |
| `KrakenExecutor` | Kraken-specific live execution (REST + CLI) |
| `UniswapExecutor` | Uniswap Trading API (DEX on Base) |
| `SmartOrderRouter` | Multi-venue quote comparison (5 venues) |
| `TWAPExecutor` | Time-weighted average price slicing |
| `IcebergExecutor` | Hidden large order execution |
| `ExecutionQualityTracker` | Transaction cost analysis |

## Safety Systems

| Component | Function |
|-----------|----------|
| `KillSwitch` | Emergency halt all trading |
| `CircuitBreaker` | Auto-halt on drawdown/volatility spike |
| `PositionFlattener` | Force-close all positions |
| `PositionManager` | Trailing/hard stop management |
| `Reconciler` | Balance reconciliation vs exchange |

## Infrastructure

| Component | Function |
|-----------|----------|
| `AgentSupervisor` | Health monitoring, auto-restart, heartbeats |
| `Checkpoint` | State checkpointing for crash recovery |
| `AgentMemory` | Cross-session learning (SOUL.md) |
| `Auditor` | SQLite/Postgres trade audit trail |
| `DataQualityMonitor` | Feed freshness and consistency checks |
| `TransactionCostAnalyzer` | Post-trade execution quality |
| `WebDashboard` | Browser UI with WebSocket streaming |
| `Dashboard` | Terminal TUI dashboard |
| `AgentGateway` | HTTP/WebSocket bridge for external AI agents |
