# SWARM TRADE — Agent Reference

120+ cooperative AI agents organized by function. All communicate via the async event `Bus` (221 topics, 69 signal channels). Every trade intent is intercepted by the AI Commander (HermesBrain + CommanderGate) before execution.

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

## Signal Intelligence (Phases 1-3)

| Agent | Function | Signal | Source |
|-------|----------|--------|--------|
| `AdaptiveKalmanFilter` | Regime-adaptive noise removal on all signals | `signal.filtered.*` | KalmanGuard |
| `DataFusionPipeline` | Cross-source convergence scoring with independence weighting | `signal.fusion` | CookFi, velox |
| `AdversarialDebateEngine` | BullAgent vs BearAgent argue before every trade | `signal.debate` | Alpha Dawg |
| `ELOTracker` | K=32 reputation tracking, agents ranked by profitable debate outcomes | — | Alpha Dawg |
| `SwarmConsensus` | N-agent weighted voting on trade decisions | `signal.consensus` | DIVE, Uniforum |
| `NarrativeEngine` | Correlates raw events into market stories for AI reasoning | `signal.narrative` | GhostFi |
| `SentimentDerivativesEngine` | RSI/MACD on sentiment itself, price-sentiment divergence | `signal.sentiment_deriv` | PortfolAI |
| `CorrelationRegimeDetector` | Cross-asset correlation matrix + regime transitions | `signal.regime_v2` | Contragent |

## Strategy + Commander

| Component | Role |
|-----------|------|
| `Strategist` | 48 weighted signal inputs, 3 regime profiles (trending/mean_rev/volatile), Kelly sizing |
| `HermesBrain` | LLM brain (Ollama/OpenAI/Anthropic/Groq/DeepSeek) — compiles market briefs, generates decisions |
| `CommanderGate` | **Sole trading authority** — intercepts ALL intents, no trade without LLM approval |
| `RiskAgent` | Quorum risk consensus from all 15 risk layers |
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
| `CommanderGate` | **AI Commander** — no trade without LLM approval (strict/conservative/passthrough) |
| `PriceValidationGate` | Multi-source price check (SAFE/WARN/HALT) before every execution |
| `ExecutionSandbox` | Agents can trade but CANNOT withdraw (TRADE_ONLY permission level) |
| `RugpullDetector` | Contract age, liquidity, ownership, honeypot screening |
| `KillSwitch` | Emergency halt all trading (file touch or button) |
| `CircuitBreaker` | Auto-halt on drawdown/volatility spike/consecutive losses |
| `PositionFlattener` | Force-close all positions on circuit breaker |
| `LiquidationShield` | Health factor monitoring with auto-deleverage before cascade |
| `PositionManager` | Trailing/hard stop management + max hold time |
| `Reconciler` | Balance reconciliation vs exchange (60s interval) |
| `HardwareSigningPipeline` | Hot wallet / Ledger USB / Secure Enclave signing |
| `AgentPolicyEngine` | Per-agent spending limits + daily caps |
| `MEVEngine` | Protect trades from frontrunning via Flashbots |

## DeFi / On-Chain (Phases 4-10)

| Component | Function | Source |
|-----------|----------|--------|
| `VaultManager` | ERC-4626 standard vault — deposit, withdraw, NAV tracking, token allowlist | Gorillionaire |
| `AgentMarketplace` | Competitive signal auctions — agents bid, highest-conviction wins | Grand Bazaar, Nimble |
| `FlashLoanExecutor` | Zero-capital arb via Aave V3 with fork simulation | CentoAI |
| `StrategyNFTManager` | Mint, fork, and trade strategies as on-chain NFTs with royalty chains | GhostFi |
| `V4HookManager` | Uniswap v4 LP management, TWAP routing, dynamic fees | GammaHedge |
| `CrossChainCoordinator` | Circle CCTP for USDC bridging, multi-chain yield optimization | Colony, Hubble |
| `CommitRevealEngine` | ZK commit-reveal dark pool with sealed-bid batch matching | CipherPool |
| `ERC8004Pipeline` | On-chain agent identity, reputation, EIP-712 intent signing | — |
| `X402PaymentGateway` | Agent-to-agent USDC micropayments | — |
| `LPRebalanceManager` | Uniswap v3 LP range rebalancing (UniRange pattern) | — |
| `YieldAggregator` | DeFi Llama yield discovery + auto-harvest | — |
| `AgentRegistry` | Unstoppable Domains agent identity on Base chain | — |

## Advanced Trading (Phases 11-20)

| Component | Function | Source |
|-----------|----------|--------|
| `RugpullDetector` | Contract safety screening — age, liquidity, ownership, honeypot | Gorillionaire, Pietro |
| `PredictionTrader` | Autonomous Polymarket execution with Kelly-sized bets | Clawlogic, Alphamarkets |
| `AgentPaymentProtocol` | Micropayment protocol with budget management + batch settlement | Alpha Dawg, Hubble |
| `StrategyEvolution` | Genetic algorithm — population of 50, crossover, mutation, tournament | Vault Royale |
| `AIBrain` | Multi-model LLM adapter (Claude/GPT/Groq/DeepSeek/local) | GhostFi, TWAP CHOP |
| `WhaleMirrorAgent` | Auto-copy smart money with signal alignment + reliability checks | Whal-E, MirrorBattle |
| `IntentSolverNetwork` | 5 competing solvers for best execution (Uniswap, Aero, Sushi, 1inch, cross-DEX) | Nimble |
| `LiquidationShield` | Health factor monitoring, auto-deleverage at danger threshold | GammaHedge, Rescue.ETH |
| `GovernanceDAO` | ELO-weighted voting on parameter changes, strategy activation, fees | Synapse Treasury |

## Interfaces (Phases 21-22)

| Component | Function | Source |
|-----------|----------|--------|
| `TelegramTradingBot` | /trade, /status, /signals, /risk, /agents, /kill, /alerts commands | ElizaTrade, NOVA |
| `VoiceTradingEngine` | "Buy 500 dollars of ETH" via Whisper STT or browser Speech API | EchoFi, Orchestra |

## Infrastructure (Phases 23-40)

| Component | Function | Source |
|-----------|----------|--------|
| `AgentFactory` | Dynamic agent spawning + lifecycle (TTL, health, promotion) | Nimble, AgentHive |
| `OptionsEngine` | Covered calls, protective puts, straddles, iron condors, vol arb | VeraFi, OpSwap |
| `SocialAlphaScanner` | Cross-platform virality scoring + coordination detection | pochi.po, Trendy-Tokens-AI |
| `BacktestingArena` | Competitive strategy tournaments ranked by risk-adjusted returns | Vault Royale, ScampIA |
| `ObservabilityDashboard` | Agent health monitoring, anomaly detection, system metrics | Aegis Agent |
| `FederatedCoordinator` | Privacy-preserving model sharing (FedAvg + differential privacy) | Colony, HiveMind |
| `RWABridge` | Tokenized real-world assets (T-bills, commodities, equities) | Radegast, VERA |
| `AutonomousTreasury` | Multi-asset allocation, yield optimization, runway tracking | ZeroKey Treasury |
| `MEVEngine` | MEV detection, capture, redistribution, and trade protection | meva, MEVAMM |
| `AgentMemoryDAG` | Long-term learning with memory DAG + semantic retrieval + decay | Alpha Dawg |
| `GridTradingEngine` | Arithmetic, geometric, and exponential grid strategies | SuperDCABOT |
| `TokenSniper` | Early token launch detection + safe entry with rugpull screening | BouncerAI |
| `GasOptimizer` | Gas price forecasting + transaction batching + priority fee tuning | AI Gas Forecaster |
| `PortfolioInsurance` | Automated hedging via perp shorts, regime-adaptive coverage | Arcare, DeltaFX |
| `AgentCommunicationProtocol` | Structured request/response messaging + capability discovery | Geneva, CrewKit |

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

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **swarmtrader** (5146 symbols, 14364 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/swarmtrader/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/swarmtrader/context` | Codebase overview, check index freshness |
| `gitnexus://repo/swarmtrader/clusters` | All functional areas |
| `gitnexus://repo/swarmtrader/processes` | All execution flows |
| `gitnexus://repo/swarmtrader/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:
1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## Keeping the Index Fresh

After committing code changes, the GitNexus index becomes stale. Re-run analyze to update it:

```bash
npx gitnexus analyze
```

If the index previously included embeddings, preserve them by adding `--embeddings`:

```bash
npx gitnexus analyze --embeddings
```

To check whether embeddings exist, inspect `.gitnexus/meta.json` — the `stats.embeddings` field shows the count (0 means no embeddings). **Running analyze without `--embeddings` will delete any previously generated embeddings.**

> Claude Code users: A PostToolUse hook handles this automatically after `git commit` and `git merge`.

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
