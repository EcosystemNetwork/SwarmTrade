# SWARM TRADE

**Multi-agent autonomous trading platform** — 80+ cooperative AI agents performing real-time market analysis, risk management, and execution across CEX + DEX venues on 3 chains.

```
                         ┌──────────────────────────────────────────────────────────────┐
                         │                    SWARM TRADE ARCHITECTURE                   │
                         └──────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────┐
    │       DATA LAYER            │     ┌────────────────────────────────────────────────┐
    │                             │     │              SIGNAL AGENTS (80+)                │
    │  Kraken (REST + WS v2)      │     │                                                │
    │  Hyperliquid (perps)        │     │  Core: Momentum, MeanRev, Volatility           │
    │  Jupiter (Solana DEX)       │────▶│  TA:   RSI, MACD, Bollinger, VWAP, Ichimoku    │
    │  BirdEye (Solana analytics) │     │  Adv:  OrderBook, Funding, Spread, Regime       │
    │  Pyth (oracle prices)       │     │  ML:   GradientBoosted trees (stdlib-only)      │
    │  MockScout (GBM sim)        │     │  Intel: Whale, SmartMoney, OnChain, FearGreed   │
    │  6 DEXs (Sushi, Aero, etc.) │     │  Ext:  News, PRISM, RSS, Social, Polymarket    │
    └─────────────────────────────┘     │  Multi: Correlation, MTF, Confluence, Alpha     │
                                        │  Feeds: Options, Macro, Stablecoin, TokenUnlock │
                                        └───────────────────┬────────────────────────────┘
                                                            │ signals
                                                            ▼
    ┌─────────────────────────────────────────────────────────────────────────────────────┐
    │                       STRATEGIST / HERMES LLM BRAIN                                  │
    │  Regime-aware adaptive weighting │ Kelly criterion │ NLP strategy parsing             │
    │  Optional local LLM (Ollama) replaces rule-based strategy with Hermes AI brain       │
    └───────────────────────────────────────────────┬─────────────────────────────────────┘
                                                    │ trade intents
                                                    ▼
    ┌─────────────────────────────────────────────────────────────────────────────────────┐
    │                         RISK PIPELINE (15 layers)                                    │
    │  Size │ Allowlist │ Drawdown │ RateLimit │ Positions │ Funds │ Allocation │ Depth    │
    │  VaR (3 methods) │ Stress Test │ Compliance │ Factor Exposure │ Rebalance │ SOR     │
    │  Agent Policy (per-agent spending limits)                                            │
    └───────────────────────────────────────────────┬─────────────────────────────────────┘
                                                    │ approved intents
                                                    ▼
    ┌─────────────────────────────────────────────────────────────────────────────────────┐
    │                         EXECUTION LAYER                                              │
    │  SmartOrderRouter (5 CEX + 10 DEX) │ TWAP │ Iceberg │ Almgren-Chriss optimal exec   │
    │  KrakenExecutor │ HyperliquidExecutor │ JupiterExecutor │ UniswapExecutor            │
    │  ArbScanner + ArbExecutor (cross-venue) │ Hardware Signing Pipeline                  │
    └───────────────────────────────────────────────┬─────────────────────────────────────┘
                                                    │
                    ┌───────────────────────────────┼───────────────────────────────┐
                    ▼                               ▼                               ▼
    ┌───────────────────────┐   ┌───────────────────────────────┐   ┌──────────────────────┐
    │    SAFETY SYSTEMS     │   │     QUANTITATIVE RISK         │   │    INFRASTRUCTURE    │
    │  KillSwitch           │   │  VaR Engine (Hist/Para/MC)    │   │  Web Dashboard       │
    │  CircuitBreaker       │   │  Stress Testing (8 scenarios) │   │  Terminal TUI        │
    │  PositionFlattener    │   │  Factor Model + PnL Attrib    │   │  AgentSupervisor     │
    │  PositionManager      │   │  Portfolio Optimization       │   │  Agent Gateway       │
    │  HardwareSigner       │   │  Compliance Suite             │   │  Social Trading      │
    │  AgentPolicies        │   │  Agent Learning System        │   │  x402 Payments       │
    │  Reconciliation       │   │  Walk-Forward Backtesting     │   │  Agent Registry      │
    └───────────────────────┘   └───────────────────────────────┘   └──────────────────────┘
```

## Key Features

- **80+ cooperative agents** — momentum, mean reversion, RSI, MACD, Bollinger, VWAP, Ichimoku, order book imbalance, funding rates, whale tracking, smart money wallet shadowing, on-chain analytics, social sentiment, news, Polymarket predictions, ML gradient boosting, alpha swarm, and more
- **15-layer risk pipeline** — every trade must pass size, drawdown, VaR, stress test, compliance, factor exposure, agent policy, and more before execution
- **Multi-chain execution** — Kraken (CEX), Hyperliquid (perps), Jupiter (Solana), Uniswap (Base), plus 6 DEX quoters (SushiSwap, Aerodrome, Curve, PancakeSwap, Raydium, Orca)
- **Hermes LLM brain** — optional local LLM (via Ollama) replaces the rule-based Strategist, controlling all 80+ agents as its eyes and ears
- **Quantitative risk engine** — VaR (historical, parametric, Monte Carlo), stress testing with 8 historical scenarios, factor model with PnL attribution
- **Portfolio optimization** — Markowitz, risk parity, Black-Litterman, dynamic hedging — all in pure Python (no numpy/scipy)
- **Smart order routing** — multi-venue quote comparison across 5 CEXs (Kraken, Binance, Coinbase, OKX, Bybit) + 10 DEXs across 3 chains
- **Cross-venue arbitrage** — ArbScanner detects price discrepancies, ArbExecutor simultaneously buys cheap + sells expensive
- **ML pipeline** — gradient-boosted decision trees trained online, feature engineering with 15+ indicators — pure stdlib
- **Social trading** — copy trading, agent leaderboard, strategy marketplace with revenue sharing
- **Alpha swarm** — multi-agent opportunity detection pipeline (AlphaHunter → SentimentFilter → RiskScreener → SwarmCoordinator)
- **Agent learning system** — online performance tracking, exploration/exploitation, dynamic signal weighting
- **Yield aggregation** — DeFi Llama integration for auto-discovering and harvesting top yield pools
- **Production safety** — kill switch, circuit breaker, position flattener, wash trade detection, balance reconciliation, agent spending policies, hardware wallet signing
- **Smart money tracking** — shadow 25+ whale wallets across Ethereum + Base chains
- **Prediction markets** — Polymarket CLOB integration for event-driven trading signals
- **LP management** — automated Uniswap v3 range rebalancing (UniRange pattern)
- **x402 payments** — agent-to-agent USDC micropayment gateway
- **Real-time dashboards** — web UI with WebSocket streaming + terminal TUI
- **Agent gateway** — HTTP/WebSocket bridge for external AI agents (OpenClaw/Hermes protocols)
- **ERC-8004 intents** — EIP-712 signed trade intents for trustless on-chain execution
- **Pyth oracle** — decentralized price feed redundancy for price validation
- **Cross-session learning** — AgentMemory reads/writes SOUL.md + strategy_notes.md across sessions
- **Walk-forward backtesting** — 10 strategies with Monte Carlo simulation and out-of-sample validation
- **Minimal core deps** — `aiohttp`, `python-dotenv` core; `web3`, `eth-account` for blockchain; `psycopg` for Postgres

## Quick Start

```bash
# Clone
git clone https://github.com/EcosystemNetwork/swarmtrader.git
cd swarmtrader

# Install
pip install -e ".[dev]"

# Copy environment config
cp .env.example .env
# Edit .env with your API keys (optional — mock mode needs none)

# Run in mock mode (no API keys needed)
python -m swarmtrader.main mock 120

# Run with web dashboard
python -m swarmtrader.main mock 300 --web --dashboard

# Run with Hermes LLM brain (requires Ollama)
ollama pull nous-hermes2
python -m swarmtrader.main mock 300 --web --hermes

# Run with real Kraken prices (paper trading)
python -m swarmtrader.main paper 600 --pairs ETHUSD BTCUSD --ws --web

# Run demo replay (pre-recorded market scenario)
python -m swarmtrader.main mock 180 --web --demo

# Run tests
pytest tests/ -v
```

## Docker

```bash
# Build and run
docker compose up --build

# Or run directly
docker build -t swarmtrader .
docker run -p 8080:8080 --env-file .env swarmtrader mock 300 --web
```

## Trading Modes

| Mode | Prices | Execution | API Keys |
|------|--------|-----------|----------|
| `mock` | Simulated (GBM) | Dry-run with slippage model | None |
| `paper` | Real (Kraken + Hyperliquid + Jupiter) | Paper trading via Kraken CLI | Optional |
| `live` | Real (all venues) | Real orders across CEX + DEX | Required (`SWARM_LIVE_CONFIRM=I_ACCEPT_RISK`) |

## CLI Options

```
python -m swarmtrader.main [mode] [duration] [options]

Positional:
  mode                    mock, paper, or live (default: mock)
  duration                Run duration in seconds (default: 60)

Options:
  --pairs PAIR [PAIR ...]  Trading pairs (default: ETHUSD)
  --base-size USD          Base trade size (default: 500)
  --max-size USD           Max single trade (default: 2000)
  --max-drawdown USD       Daily drawdown limit (default: 200)
  --capital USD            Starting capital (default: 10000)
  --max-alloc PCT          Max allocation per asset (default: 50%)
  --max-positions N        Max open positions (default: 5)
  --hard-stop PCT          Hard stop-loss per position (default: 5%)
  --trail-stop PCT         Trailing stop (default: 3%)
  --max-hold SECS          Max hold time (default: 3600)
  --ws                     Use Kraken WebSocket v2 streaming (vs REST polling)
  --web                    Launch web dashboard on port 8080
  --web-port PORT          Web dashboard port (default: 8080)
  --dashboard              Show terminal TUI dashboard
  --gateway                Enable external agent gateway
  --demo                   Run demo replay mode with pre-recorded scenario
  --hermes                 Use local Hermes LLM (Ollama) as brain instead of rule-based Strategist
  --db PATH                SQLite database path (default: swarm.db)
  --checkpoint             Enable state checkpointing for crash recovery
  --checkpoint-path PATH   Checkpoint file path (default: swarm_checkpoint.json)
  --erc8004                Enable ERC-8004 on-chain identity and reputation
  --erc8004-network NET    ERC-8004 network: sepolia, mainnet, base (default: sepolia)
  --gateway-key KEY        Master key for agent gateway (auto-generated if omitted)
  --no-advanced            Disable advanced agents (OI, liquidation levels)
```

## Backtesting

```bash
# Walk-forward backtest with Monte Carlo
python -m swarmtrader.backtest --pair ETHUSD --walk-forward 5

# Generate HTML report from trade database
python -m swarmtrader.report --db swarm.db --html report.html
```

The built-in `BacktesterAgent` runs continuously in the background, validating 10 strategies across all traded assets with walk-forward + Monte Carlo simulation. Strategies that pass minimum Sharpe (0.5), win rate (45%), and max drawdown (25%) thresholds get promoted.

## Project Structure

```
swarmtrader/
├── core.py              # Bus, MarketSnapshot, Signal, PortfolioTracker
├── main.py              # Entry point — orchestrates 80+ agents
├── config.py            # Centralized configuration management
├── logging_config.py    # Structured JSON logging with correlation IDs
├── database.py          # Postgres (Neon) + SQLite database abstraction
│
├── ── DATA SCOUTS ──────────────────────────────────────
├── agents.py            # MockScout, Momentum, MeanRev, Volatility
├── kraken.py            # KrakenScout, KrakenWSScout, KrakenExecutor
├── kraken_api.py        # Kraken REST API v2 native client
├── kraken_ws.py         # Kraken WebSocket v2 (book, trades, executions)
├── hyperliquid.py       # Hyperliquid perps: data + execution
├── jupiter.py           # Jupiter DEX (Solana aggregator): prices + execution
├── birdeye.py           # BirdEye Solana token analytics
├── pyth_oracle.py       # Pyth Network decentralized price feeds
├── demo.py              # Demo replay scouts (pre-recorded scenarios)
│
├── ── SIGNAL AGENTS ────────────────────────────────────
├── agents_advanced.py   # OrderBook, FundingRate, Spread, Regime
├── strategies.py        # RSI, MACD, Bollinger, VWAP, Ichimoku, LiqCascade, ATRStop
├── ml_signal.py         # Gradient-boosted decision trees (pure stdlib)
├── multitf.py           # Multi-timeframe momentum alignment
├── correlation.py       # Cross-asset correlation + lead-lag analysis
├── confluence.py        # Multi-group signal agreement scoring
├── alpha_swarm.py       # AlphaHunter → SentimentFilter → RiskScreener pipeline
│
├── ── MARKET INTELLIGENCE ──────────────────────────────
├── whale.py             # Large transaction tracking
├── smart_money.py       # 25+ whale wallet shadowing (Etherscan/Basescan)
├── onchain.py           # Etherscan on-chain activity
├── feargreed.py         # Alternative.me Fear & Greed Index
├── social.py            # Social media sentiment aggregation
├── social_agents.py     # X/Twitter, Discord, Telegram real-time monitoring
├── liquidation.py       # Futures liquidation level detection
├── open_interest.py     # Futures open interest tracking
├── arbitrage.py         # Cross-exchange price difference monitoring
├── news.py              # CryptoPanic news sentiment
├── signals.py           # PRISM/Strykr AI signal provider
├── polymarket.py        # Polymarket prediction market CLOB signals
│
├── ── EXTENDED DATA FEEDS ──────────────────────────────
├── feeds.py             # 7 agents: ExchangeFlow, Stablecoin, Macro,
│                        #   DeribitOptions, TokenUnlock, GitHubDev, RSSNews
│
├── ── STRATEGY + COORDINATION ──────────────────────────
├── strategy.py          # Strategist (adaptive weighting) + RiskAgent + Coordinator
├── hermes_brain.py      # Hermes LLM brain (local Ollama inference)
├── nlp_strategy.py      # Natural language strategy parsing + presets
│
├── ── RISK MANAGEMENT ──────────────────────────────────
├── risk.py              # RateLimiter, DrawdownTracker, Concentration, Exposure
├── var.py               # VaR engine (historical, parametric, Monte Carlo)
├── stress_test.py       # 8 historical stress scenarios
├── factor_model.py      # Factor model + PnL attribution
├── compliance.py        # WashTrading, MarginMonitor, DataQuality
├── agent_policies.py    # Per-agent spending limits + sandboxing
│
├── ── PORTFOLIO + POSITIONS ────────────────────────────
├── portfolio_opt.py     # Markowitz, RiskParity, BlackLitterman, DynamicHedger
├── positions.py         # PositionManager with trailing/hard stops
├── wallet.py            # WalletManager with allocation tracking
├── capital_allocator.py # Agent leaderboard + dynamic capital allocation
│
├── ── EXECUTION ────────────────────────────────────────
├── execution.py         # Simulator, Executor, Auditor (SQLite/Postgres)
├── sor.py               # Smart order router (5 CEXs + 10 DEXs)
├── microstructure.py    # Kyle's lambda, Almgren-Chriss, Iceberg, TCA
├── twap.py              # TWAP execution (time-weighted slicing)
├── uniswap.py           # Uniswap Trading API executor (DEX on Base)
├── arb_executor.py      # ArbScanner + ArbExecutor (cross-venue simultaneous)
├── dex_quotes.py        # 1inch + Jupiter quote aggregation
├── dex_multi.py         # SushiSwap, Aerodrome, Curve, PancakeSwap, Raydium, Orca
├── exchanges.py         # Binance, Coinbase, OKX, Bybit unified API
│
├── ── SAFETY SYSTEMS ───────────────────────────────────
├── safety.py            # KillSwitch, CircuitBreaker, PositionFlattener
├── reconciliation.py    # Balance reconciliation vs exchange
├── hardware_signer.py   # Hot wallet / Ledger USB / Secure Enclave signing
│
├── ── BLOCKCHAIN + ON-CHAIN ────────────────────────────
├── erc8004.py           # ERC-8004 intent signing (EIP-712) + identity + reputation
├── x402_payments.py     # Agent-to-agent USDC micropayment gateway
├── lp_manager.py        # Uniswap v3 LP range rebalancing (UniRange pattern)
├── yield_aggregator.py  # DeFi Llama yield discovery + auto-harvest
├── agent_registry.py    # Unstoppable Domains agent identity registry
├── strategy_privacy.py  # Encrypted strategy storage
│
├── ── SOCIAL + COMMUNITY ───────────────────────────────
├── social_trading.py    # Copy trading, leaderboard, strategy marketplace
│
├── ── LEARNING + BACKTESTING ───────────────────────────
├── agent_learning.py    # Online performance tracking, exploration/exploitation
├── memory.py            # AgentMemory — cross-session learning (SOUL.md)
├── backtester.py        # 10-strategy backtesting engine (walk-forward + Monte Carlo)
├── backtest.py          # Historical replay engine
├── walkforward.py       # Walk-forward + Monte Carlo backtesting framework
├── report.py            # JSON/HTML/PDF report generation
│
├── ── INFRASTRUCTURE ───────────────────────────────────
├── automation.py        # AgentSupervisor, Scheduler, heartbeats
├── dashboard.py         # Terminal TUI dashboard
├── web.py               # Web dashboard (aiohttp + WebSocket + social trading)
├── gateway.py           # Agent gateway (OpenClaw/Hermes protocols)
├── checkpoint.py        # State checkpointing for crash recovery
├── rate_limit.py        # Shared API rate limiter
│
└── static/
    └── index.html       # Web dashboard frontend (Chart.js + Tailwind)
```

## Agent Categories

| Category | Count | Agents |
|----------|-------|--------|
| Data Scouts | 7 | MockScout, KrakenScout, KrakenWSScout, HyperliquidAgent, JupiterPriceScout, BirdEyeAgent, PythOracle |
| Core Analysts | 3 | Momentum, MeanReversion, Volatility |
| Technical Analysis | 7 | RSI, MACD, Bollinger, VWAP, Ichimoku, LiquidationCascade, ATRTrailingStop |
| Advanced Market | 4 | OrderBook, FundingRate, Spread, Regime |
| Market Intelligence | 9 | Whale, SmartMoney, OnChain, FearGreed, Social, Liquidation, Arbitrage, OpenInterest, Polymarket |
| External Signals | 2 | News (CryptoPanic), PRISM AI |
| Extended Feeds | 7 | ExchangeFlow, Stablecoin, MacroCalendar, DeribitOptions, TokenUnlock, GitHubDev, RSSNews |
| Cross-Asset | 3 | MultiTimeframe, Correlation, Confluence |
| Alpha Swarm | 4 | AlphaHunter, SentimentFilter, RiskScreener, SwarmCoordinator |
| Machine Learning | 1 | GradientBoosted (online training) |
| Social Media | 4 | XMonitor (Twitter), Discord, Telegram, SocialAggregator |
| Portfolio | 2 | PortfolioOpt, DynamicHedger |
| Risk Agents | 15 | Size, Drawdown, VaR, Stress, Compliance, Factor, Rebalance, SOR, AgentPolicy, etc. |
| Execution | 8 | Simulator, KrakenExecutor, HyperliquidExecutor, JupiterExecutor, UniswapExecutor, SOR, TWAP, Iceberg |
| Arbitrage | 3 | ArbScanner, ArbExecutor, MultiDEXScanner |
| Yield/LP | 2 | YieldAggregator, LPRebalanceManager |
| Safety | 5 | KillSwitch, CircuitBreaker, PositionFlattener, HardwareSigner, AgentPolicies |
| Learning | 2 | LearningCoordinator, BacktesterAgent |
| Infrastructure | 7 | Supervisor, Reconciler, DataQuality, TCA, AgentRegistry, x402Payments, Checkpoint |

## Multi-Chain Execution

| Chain | Venue | Type | Data | Execution | Key Required |
|-------|-------|------|------|-----------|-------------|
| — | Kraken | CEX | REST + WS v2 | Paper / Live | `KRAKEN_API_KEY` |
| — | Binance | CEX | Quotes | SOR routing | Optional |
| — | Coinbase | CEX | Quotes | SOR routing | Optional |
| — | OKX | CEX | Quotes | SOR routing | Optional |
| — | Bybit | CEX | Quotes | SOR routing | Optional |
| Ethereum / Base | Uniswap | DEX | Quotes | Swaps | `UNISWAP_API_KEY` + `PRIVATE_KEY` |
| Multiple | Hyperliquid | Perps DEX | Full L2 + funding | Orders | `HYPERLIQUID_WALLET_KEY` |
| Solana | Jupiter | DEX | Aggregated quotes | Swaps | `SOLANA_PRIVATE_KEY` |
| Multiple | SushiSwap | DEX | Quotes | Via SOR | — |
| Base | Aerodrome | DEX | Quotes | Via SOR | — |
| Multiple | Curve | DEX | Quotes | Via SOR | — |
| BSC | PancakeSwap | DEX | Quotes | Via SOR | — |
| Solana | Raydium | DEX | Quotes | Via SOR | — |
| Solana | Orca | DEX | Quotes | Via SOR | — |

## Extended Data Feeds

7 additional market intelligence agents run automatically alongside the core swarm:

| Agent | Intelligence | API Key | Interval |
|-------|-------------|---------|----------|
| ExchangeFlow | Exchange reserve inflow/outflow via CoinGecko | `COINGECKO_API_KEY` (optional) | 5 min |
| Stablecoin | USDT/USDC market cap + depeg detection | `COINGECKO_API_KEY` (optional) | 10 min |
| MacroCalendar | Economic events (FOMC, CPI, NFP) | `FRED_API_KEY` (optional) | 30 min |
| DeribitOptions | Options IV, put/call ratio, max pain (BTC/ETH) | None (public) | 5 min |
| TokenUnlock | Token vesting unlock schedule via DeFi Llama | None | 60 min |
| GitHubDev | Protocol commit velocity + releases | `GITHUB_TOKEN` (optional) | 30 min |
| RSSNews | Multi-source RSS (CoinDesk, CoinTelegraph, Decrypt) | None | 5 min |

## Hermes LLM Brain

When `--hermes` is passed, a local LLM (via Ollama) replaces the rule-based Strategist:

```bash
# Pull the model first
ollama pull nous-hermes2

# Run with Hermes brain
python -m swarmtrader.main mock 300 --web --hermes
```

The Hermes brain receives a structured context window containing all 80+ agent signals, portfolio state, risk metrics, and market regime — then generates trade decisions in a structured JSON format. The rule-based Strategist runs as a backup alongside Hermes.

Configure via `.env`:
- `OLLAMA_URL` — Ollama API endpoint (default: `http://localhost:11434`)
- `OLLAMA_MODEL` — Model name (default: `nous-hermes2`)
- `HERMES_INTERVAL` — Seconds between LLM calls (default: 15)
- `HERMES_MAX_TOKENS` — Max response tokens (default: 1024)

## Risk Pipeline (15 Layers)

Every trade intent must pass ALL risk checks before execution:

| # | Check | Description |
|---|-------|-------------|
| 1 | `size_check` | Rejects orders exceeding max single trade size |
| 2 | `allowlist_check` | Only trade pre-approved asset pairs |
| 3 | `drawdown_check` | Halt when daily loss exceeds configurable limit |
| 4 | `rate_limit_check` | Max 20 trades/hour (configurable) |
| 5 | `max_positions_check` | Cap concurrent open positions |
| 6 | `funds_check` | Verify sufficient funds in wallet |
| 7 | `allocation_check` | Per-asset allocation cap enforcement |
| 8 | `depth_liquidity_check` | Order book depth must support trade size |
| 9 | `var_check` | Portfolio VaR (historical, parametric, Monte Carlo) must stay under threshold |
| 10 | `stress_check` | Trade must survive 8 historical stress scenarios |
| 11 | `compliance_check` | Wash trade detection + margin monitoring |
| 12 | `factor_exposure_check` | Factor model exposure limits |
| 13 | `rebalance_check` | Portfolio drift trigger check |
| 14 | `sor_venue_check` | Minimum 2 venues must provide quotes |
| 15 | `agent_policy_check` | Per-agent daily spending cap enforcement |

## Advanced Features

- **Cross-venue arbitrage** — `ArbScanner` polls all CEXs + DEXs every 10s for price discrepancies >15bps. `ArbExecutor` simultaneously buys on cheap venue + sells on expensive venue.
- **Alpha swarm** — Multi-agent opportunity detection: `AlphaHunter` scans for breakout/dip/accumulation patterns → `SentimentFilter` validates with social/news data → `RiskScreener` applies risk rules → `SwarmCoordinator` executes with minimum conviction threshold.
- **Smart money tracking** — Shadows 25+ whale wallets via Etherscan + Basescan APIs. Detects accumulation/distribution patterns with configurable copy thresholds.
- **Polymarket signals** — Reads prediction market contracts for event-driven signals (BTC price targets, regulatory events, ETF flows).
- **Yield aggregation** — Scans DeFi Llama for top yield pools across Ethereum, Base, and Arbitrum. Auto-compounds when harvest threshold reached.
- **LP management** — Monitors Uniswap v3 concentrated liquidity positions. Auto-rebalances when price moves beyond configured range (500bps default).
- **Agent learning** — `LearningCoordinator` tracks each agent's signal accuracy over time. Uses exploration/exploitation to weight reliable agents higher.
- **Hardware signing** — Three modes: hot wallet (default), Ledger USB, or secure enclave. Configurable USD threshold for hardware approval.
- **x402 payments** — Agent-to-agent USDC micropayment gateway for paid signal services.
- **Social trading** — Users can publish strategies, follow top performers, and auto-copy trades with configurable allocation and revenue sharing.
- **Crash recovery** — `--checkpoint` saves state periodically to JSON. On restart, resumes from last checkpoint automatically.
- **Cross-session learning** — `AgentMemory` reads `SOUL.md` (immutable identity/principles) and writes to `strategy_notes.md` after each session.
- **Agent registry** — On-chain agent identity via Unstoppable Domains naming on Base chain.

## Environment Configuration

All environment variables are documented in `.env.example`. Key categories:

| Category | Variables | Required |
|----------|-----------|----------|
| Kraken exchange | `KRAKEN_API_KEY`, `KRAKEN_PRIVATE_KEY`, `KRAKEN_TIER` | Paper/live only |
| Database | `DATABASE_URL`, `SWARM_DB_PATH` | No (SQLite fallback) |
| Hyperliquid | `HYPERLIQUID_WALLET_KEY`, `HYPERLIQUID_VAULT_ADDRESS` | No (data-only without) |
| Jupiter / Solana | `SOLANA_PRIVATE_KEY`, `SOLANA_WALLET_ADDRESS`, `SOLANA_RPC_URL` | No (prices-only without) |
| BirdEye | `BIRDEYE_API_KEY` | No (disabled without) |
| Blockchain | `PRIVATE_KEY`, `UNISWAP_API_KEY`, `UNISWAP_CHAIN_ID` | ERC-8004/DEX only |
| Hermes LLM | `OLLAMA_URL`, `OLLAMA_MODEL`, `HERMES_INTERVAL` | No (rule-based default) |
| Social media | `X_BEARER_TOKEN`, `DISCORD_WEBHOOK_URL`, `TELEGRAM_BOT_TOKEN` | No |
| Risk tuning | `SWARM_RISK_MAX_DAILY_LOSS`, `SWARM_RISK_MAX_TRADES`, etc. | No (sensible defaults) |
| Circuit breaker | `SWARM_CB_MAX_DRAWDOWN`, `SWARM_CB_COOLDOWN` | No |
| Execution | `SWARM_MODE`, `SWARM_EXEC_MAX_RETRIES` | No |
| Extended feeds | `GITHUB_TOKEN`, `FRED_API_KEY`, `ETHERSCAN_API_KEY`, `COINGECKO_API_KEY` | No (graceful degradation) |
| Hardware signer | `HARDWARE_SIGNER_MODE`, `HARDWARE_APPROVE_USD` | No (hot wallet default) |
| Agent policies | `AGENT_POLICY_STRICT`, `AGENT_POLICY_DEFAULT_DAILY_CAP` | No |
| Agent registry | `UD_API_KEY`, `AGENT_REGISTRY_CHAIN` | No |
| Yield/LP | `YIELD_MIN_APY`, `LP_RANGE_BPS` | No |

See [`.env.example`](.env.example) for the full list with defaults and descriptions.

## Supported Assets

The system supports multi-asset trading with pre-configured Kraken mappings:

| Asset | Spot Pair | Futures Symbol |
|-------|-----------|----------------|
| ETH | ETHUSD | PF_ETHUSD |
| BTC | XBTUSD | PF_XBTUSD |
| SOL | SOLUSD | PF_SOLUSD |
| XRP | XRPUSD | PF_XRPUSD |
| ADA | ADAUSD | PF_ADAUSD |
| DOT | DOTUSD | PF_DOTUSD |
| LINK | LINKUSD | PF_LINKUSD |
| AVAX | AVAXUSD | PF_AVAXUSD |

Jupiter/Solana tokens: SOL, JUP, BONK, WIF, PYTH, JTO, RAY

## Technology

- **Language**: Python 3.11+ (async/await throughout)
- **Architecture**: Event-driven pub/sub via async `Bus`
- **Runtime deps**: `aiohttp`, `python-dotenv`, `web3`, `eth-account`, `psycopg`, `psycopg-pool`
- **ML/Math**: All implemented from scratch — no numpy, scipy, sklearn, or ta-lib
- **CEX**: Kraken (REST v2 + WebSocket v2 + CLI), Binance, Coinbase, OKX, Bybit
- **DEX**: Uniswap (Base), Jupiter (Solana), Hyperliquid, SushiSwap, Aerodrome, Curve, PancakeSwap, Raydium, Orca
- **Oracle**: Pyth Network (decentralized price feeds)
- **Blockchain**: ERC-8004 intents on Ethereum/Base (Sepolia testnet)
- **Storage**: Postgres (Neon) primary, SQLite fallback for offline/backtest
- **Frontend**: Tailwind CSS + Chart.js + vanilla JS
- **LLM**: Ollama (optional, for Hermes brain)
- **Deployed**: Railway (backend) + Neon Postgres (database)

## Documentation

| File | Contents |
|------|----------|
| [`README.md`](README.md) | This file — setup, architecture, features |
| [`AGENTS.md`](AGENTS.md) | Complete agent reference with signals and categories |
| [`SOUL.md`](SOUL.md) | Agent identity, principles, and risk discipline |
| [`ROADMAP.md`](ROADMAP.md) | 20-phase product roadmap with revenue projections |
| [`COMPETITIVE_INTEL.md`](COMPETITIVE_INTEL.md) | Competitive analysis of 450+ ETHGlobal projects |
| [`VIDEO_SCRIPT.md`](VIDEO_SCRIPT.md) | Demo video presentation script |
| [`.env.example`](.env.example) | All environment variables with descriptions |
| [`strategy_notes.md`](strategy_notes.md) | Auto-generated cross-session learning notes |

## License

MIT
