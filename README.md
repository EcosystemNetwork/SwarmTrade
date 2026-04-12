# SWARM TRADE

**Multi-agent autonomous trading platform** — 50+ cooperative AI agents performing real-time market analysis, risk management, and execution through an async event-driven architecture.

```
                         ┌──────────────────────────────────────────────────────────────┐
                         │                    SWARM TRADE ARCHITECTURE                   │
                         └──────────────────────────────────────────────────────────────┘

    ┌─────────────────────────────┐
    │       DATA LAYER            │     ┌────────────────────────────────────────────────┐
    │                             │     │              SIGNAL AGENTS (50+)                │
    │  KrakenScout (REST)         │     │                                                │
    │  KrakenWSScout (WebSocket)  │────▶│  Core: Momentum, MeanRev, Volatility           │
    │  MockScout (GBM sim)        │     │  TA:   RSI, MACD, Bollinger, VWAP, Ichimoku    │
    │                             │     │  Adv:  OrderBook, Funding, Spread, Regime       │
    └─────────────────────────────┘     │  ML:   GradientBoosted trees (stdlib-only)      │
                                        │  Intel: Whale, OnChain, FearGreed, Sentiment    │
                                        │  Ext:  News, PRISM AI, Arbitrage, Liquidation   │
                                        │  Multi: Correlation, MTF, Confluence            │
                                        └───────────────────┬────────────────────────────┘
                                                            │ signals
                                                            ▼
    ┌─────────────────────────────────────────────────────────────────────────────────────┐
    │                               STRATEGIST                                            │
    │  Regime-aware adaptive weighting │ Kelly criterion sizing │ Quorum risk consensus    │
    └───────────────────────────────────────────────┬─────────────────────────────────────┘
                                                    │ trade intents
                                                    ▼
    ┌─────────────────────────────────────────────────────────────────────────────────────┐
    │                          RISK PIPELINE (15 layers)                                   │
    │  Size │ Allowlist �� Drawdown │ RateLimit │ Positions │ Funds │ Allocation │ Depth    │
    │  VaR (3 methods) │ Stress Test │ Compliance │ Factor Exposure │ Rebalance │ SOR     │
    └───────────────────────────────────────────────┬─────────────────────────────────────┘
                                                    │ approved intents
                                                    ▼
    ┌─────────────────────────────────────────────────────────────────────────────────────┐
    │                          EXECUTION LAYER                                             │
    │  SmartOrderRouter (5 venues) │ TWAP │ Iceberg │ Almgren-Chriss optimal execution     │
    │  KrakenExecutor (live) │ Simulator (dry-run) │ TCA tracking                          │
    └───────────────────────────────────────────────┬─────────────────────────────────────┘
                                                    │
                    ┌───────────────────────────────┼───────────────────────────────┐
                    ▼                               ▼                               ▼
    ┌───────────────────────┐   ┌───────────────────────────────┐   ┌──────────────────────┐
    │    SAFETY SYSTEMS     │   │     QUANTITATIVE RISK         │   │    MONITORING        │
    │  KillSwitch           │   │  VaR Engine (Hist/Para/MC)    │   │  Web Dashboard       │
    │  CircuitBreaker       │   │  Stress Testing (8 scenarios) │   │  Terminal TUI        │
    │  PositionFlattener    │   │  Factor Model + PnL Attrib    │   │  Agent Supervisor    │
    │  PositionManager      │   │  Portfolio Optimization       │   │  SQLite Audit Trail  │
    │  Reconciliation       │   │  Compliance Suite             │   │  Agent Gateway       │
    └───────────────────────┘   └───────────────────────────────┘   └──────────────────────┘
```

## Key Features

- **50+ cooperative agents** — momentum, mean reversion, RSI, MACD, Bollinger, VWAP, Ichimoku, order book imbalance, funding rates, whale tracking, on-chain analytics, social sentiment, news, ML gradient boosting
- **15-layer risk pipeline** — every trade must pass size, drawdown, VaR, stress test, compliance, factor exposure, and more before execution
- **Quantitative risk engine** — VaR (historical, parametric, Monte Carlo), stress testing with 8 historical scenarios, factor model with PnL attribution
- **Portfolio optimization** — Markowitz, risk parity, Black-Litterman, dynamic hedging — all in pure Python (no numpy/scipy)
- **Smart order routing** — multi-venue quote comparison across Kraken, Binance, Coinbase, OKX, dYdX
- **ML pipeline** — gradient-boosted decision trees trained online, feature engineering with 15+ indicators — pure stdlib
- **Production safety** — kill switch, circuit breaker, position flattener, wash trade detection, balance reconciliation
- **Real-time dashboards** — web UI with WebSocket streaming + terminal TUI
- **Agent gateway** — HTTP/WebSocket bridge for external AI agents (OpenClaw/Hermes protocols)
- **ERC-8004 intents** — EIP-712 signed trade intents for trustless on-chain execution
- **Minimal dependencies** — `aiohttp`, `python-dotenv` core; `web3`, `eth-account` for blockchain; `psycopg` for Postgres

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
| `paper` | Real (Kraken) | Paper trading via Kraken CLI | Optional |
| `live` | Real (Kraken) | Real orders on Kraken | Required |

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
  --ws                     Use WebSocket streaming (vs REST polling)
  --web                    Launch web dashboard on port 8080
  --web-port PORT          Web dashboard port (default: 8080)
  --dashboard              Show terminal TUI dashboard
  --gateway                Enable external agent gateway
  --demo                   Run demo replay mode with pre-recorded scenario
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
# Walk-forward backtest
python -m swarmtrader.backtest --pair ETHUSD --walk-forward 5

# Generate HTML report from trade database
python -m swarmtrader.report --db swarm.db --html report.html
```

## Project Structure

```
swarmtrader/
├── core.py              # Bus, MarketSnapshot, Signal, PortfolioTracker
├── main.py              # Entry point — orchestrates 50+ agents
├── agents.py            # MockScout, Momentum, MeanRev, Volatility
├── agents_advanced.py   # OrderBook, FundingRate, Spread, Regime
├── strategies.py        # RSI, MACD, Bollinger, VWAP, Ichimoku
├── strategy.py          # Strategist (adaptive weighting) + RiskAgent + Coordinator
├── execution.py         # Simulator, Executor, Auditor (SQLite)
├── kraken.py            # KrakenScout, KrakenWSScout, KrakenExecutor
├── positions.py         # PositionManager with trailing/hard stops
├── safety.py            # KillSwitch, CircuitBreaker, PositionFlattener
├── risk.py              # RateLimiter, DrawdownTracker, Concentration, Exposure
├── var.py               # VaR engine (historical, parametric, Monte Carlo)
├── stress_test.py       # 8 historical stress scenarios
├── portfolio_opt.py     # Markowitz, RiskParity, BlackLitterman, DynamicHedger
├── factor_model.py      # Factor model + PnL attribution
├── ml_signal.py         # Gradient-boosted decision trees (pure stdlib)
├── sor.py               # Smart order router (multi-venue)
├── microstructure.py    # Kyle's lambda, Almgren-Chriss, Iceberg, TCA
├── twap.py              # TWAP execution (time-weighted slicing)
├── compliance.py        # WashTrading, MarginMonitor, DataQuality
├── reconciliation.py    # Balance reconciliation vs exchange
├── config.py            # Centralized configuration management
├── logging_config.py    # Structured JSON logging
├── wallet.py            # WalletManager with allocation tracking
├── automation.py        # AgentSupervisor, Scheduler, heartbeats
├── news.py              # CryptoPanic sentiment
├── signals.py           # PRISM AI signal provider
├── whale.py             # Large transaction tracking
├── multitf.py           # Multi-timeframe momentum
├── correlation.py       # Cross-asset correlation + lead-lag
├── confluence.py        # Multi-group signal agreement
├── walkforward.py       # Walk-forward backtesting + Monte Carlo
├── backtest.py          # Historical replay engine
├── report.py            # JSON/HTML report generation
├── dashboard.py         # Terminal TUI dashboard
├── web.py               # Web dashboard (aiohttp + WebSocket)
├── gateway.py           # Agent gateway (OpenClaw/Hermes)
├── erc8004.py           # ERC-8004 intent signing (EIP-712)
├── uniswap.py           # Uniswap Trading API executor (DEX on Base)
├── checkpoint.py        # State checkpointing for crash recovery
├── memory.py            # AgentMemory — cross-session learning (SOUL.md)
├── capital_allocator.py # Agent leaderboard + dynamic capital allocation
├── database.py          # Postgres (Neon) + SQLite database abstraction
├── feeds.py             # 7 extended data feed agents
├── demo.py              # Demo replay scouts (pre-recorded scenarios)
├── rate_limit.py        # Shared API rate limiter
└── static/
    └── index.html       # Web dashboard frontend (Chart.js + Tailwind)
```

## Agent Categories

| Category | Count | Examples |
|----------|-------|---------|
| Data Scouts | 3 | MockScout, KrakenScout, KrakenWSScout |
| Core Analysts | 3 | Momentum, MeanReversion, Volatility |
| Technical Analysis | 7 | RSI, MACD, Bollinger, VWAP, Ichimoku, LiqCascade, ATRStop |
| Advanced Market | 4 | OrderBook, FundingRate, Spread, Regime |
| Market Intelligence | 6 | Whale, OnChain, FearGreed, Social, Liquidation, Arbitrage |
| External Signals | 2 | News (CryptoPanic), PRISM AI |
| Extended Feeds | 7 | ExchangeFlow, Stablecoin, MacroCalendar, DeribitOptions, TokenUnlock, GitHubDev, RSSNews |
| Cross-Asset | 3 | MultiTimeframe, Correlation, Confluence |
| Machine Learning | 1 | GradientBoosted (online training) |
| Portfolio | 2 | PortfolioOpt, DynamicHedger |
| Risk Agents | 14 | Size, Drawdown, VaR, Stress, Compliance, etc. |
| Execution | 5 | Simulator, Executor, SOR, TWAP, Iceberg |
| Safety | 3 | KillSwitch, CircuitBreaker, PositionFlattener |
| Infrastructure | 4 | Supervisor, Reconciler, DataQuality, TCA |

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

## Advanced Features

- **Crash recovery** — `--checkpoint` saves state periodically to a JSON file. On restart, the swarm resumes from the last checkpoint automatically.
- **Cross-session learning** — `AgentMemory` system reads/writes `SOUL.md` to learn from past sessions. Strategy notes are auto-appended to `strategy_notes.md` after each run.
- **Capital allocator** — Agent leaderboard tracks signal performance and dynamically allocates capital to top-performing agents.
- **Uniswap DEX execution** — When `UNISWAP_API_KEY` and `PRIVATE_KEY` are set, adds Uniswap on Base as a real venue in the Smart Order Router.

## Environment Configuration

All environment variables are documented in `.env.example`. Key categories:

| Category | Variables | Required |
|----------|-----------|----------|
| Kraken exchange | `KRAKEN_API_KEY`, `KRAKEN_PRIVATE_KEY`, `KRAKEN_TIER` | Paper/live only |
| Database | `DATABASE_URL`, `SWARM_DB_PATH`, `SWARM_DB_*_POOL` | No (SQLite fallback) |
| Risk tuning | `SWARM_RISK_MAX_DAILY_LOSS`, `SWARM_RISK_MAX_TRADES`, etc. | No (sensible defaults) |
| Circuit breaker | `SWARM_CB_MAX_DRAWDOWN`, `SWARM_CB_COOLDOWN` | No |
| Execution | `SWARM_MODE`, `SWARM_EXEC_MAX_RETRIES`, `SWARM_DEFAULT_ORDER_TYPE` | No |
| Blockchain | `PRIVATE_KEY`, `UNISWAP_API_KEY`, `UNISWAP_CHAIN_ID` | ERC-8004/DEX only |
| Extended feeds | `GITHUB_TOKEN`, `FRED_API_KEY`, `ETHERSCAN_API_KEY`, `COINGECKO_API_KEY` | No (graceful degradation) |

See `.env.example` for the full list with defaults and descriptions.

## Hackathon

| Prize | Our Edge |
|-------|---------|
| **Best Risk-Adjusted Return** | 15-layer risk pipeline, Kelly sizing, regime-aware adaptive strategy, VaR + stress testing |
| **Best Compliance & Guardrails** | Multi-agent quorum, circuit breakers, wash trade detection, factor exposure limits, reconciliation |
| **Kraken Challenge** | Deep Kraken CLI integration: REST + WS data, paper/live execution, orderbook, funding rates, ERC-8004 intents |

## Technology

- **Language**: Python 3.11+ (async/await throughout)
- **Architecture**: Event-driven pub/sub via async `Bus`
- **Runtime deps**: `aiohttp`, `python-dotenv`, `web3`, `eth-account`, `psycopg`, `psycopg-pool`
- **ML/Math**: All implemented from scratch — no numpy, scipy, sklearn, or ta-lib
- **Exchange**: Kraken (REST + WebSocket + CLI)
- **Blockchain**: ERC-8004 intents on Ethereum (Sepolia testnet)
- **Storage**: Postgres (Neon) primary, SQLite fallback for offline/backtest
- **Frontend**: Tailwind CSS + Chart.js + vanilla JS

## License

MIT
