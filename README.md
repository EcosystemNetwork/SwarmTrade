# SWARM TRADE

**Autonomous AI trading platform** — 122 modules, 120+ cooperative agents, an AI commander that approves every trade.

```
                    ┌──────────────────────────────────────────────────────────┐
                    │                  SWARM TRADE ARCHITECTURE                │
                    └──────────────────────────────────────────────────────────┘

 ┌──────────────────────┐     ┌──────────────────────────────────────────────────┐
 │     DATA LAYER       │     │            SIGNAL AGENTS (120+)                  │
 │                      │     │                                                  │
 │  Kraken (REST+WS v2) │     │  Core: Momentum, MeanRev, Volatility             │
 │  Hyperliquid (perps)  │     │  TA:   RSI, MACD, Bollinger, VWAP, Ichimoku     │
 │  Jupiter (Solana DEX) │────>│  Adv:  OrderBook, Funding, Spread, Regime       │
 │  BirdEye + Pyth       │     │  ML:   GradientBoosted (stdlib), Kalman Filter  │
 │  6 DEXs + 5 CEXs      │     │  Intel: Whale, SmartMoney, OnChain, FearGreed   │
 │  CoinGecko, FRED      │     │  Social: News, RSS, Twitter, Telegram, Discord  │
 │  Etherscan, Basescan   │     │  Feeds: Options, Macro, Stablecoin, TokenUnlock │
 │  DeFi Llama, Deribit   │     │  Alpha: Swarm, Fusion, Debate, Consensus        │
 └──────────────────────┘     │  New:  MEV, Grid, Sniper, Sentiment Derivatives  │
                              └────────────────────┬─────────────────────────────┘
                                                   │ 59 signal topics
                                                   v
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                    SIGNAL INTELLIGENCE LAYER                                 │
 │  Kalman Filter (noise removal) -> Data Fusion (convergence scoring)         │
 │  Narrative Engine (event correlation) -> Sentiment Derivatives (sentiment TA)│
 └────────────────────────────────────────────┬─────────────────────────────────┘
                                              v
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                 STRATEGIST (48 weighted inputs, 3 regime profiles)           │
 │  Regime-aware adaptive weighting | Kelly criterion | NLP strategy parsing    │
 └────────────────────────────────────────────┬─────────────────────────────────┘
                                              v
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                      ALPHA DISCOVERY + DEBATE                                │
 │  AlphaHunter (3+ agents agree) -> SentimentFilter -> RiskScreener           │
 │  Adversarial Debate (BullAgent vs BearAgent) -> ELO Reputation              │
 │  Swarm Consensus (N-agent weighted voting)                                  │
 └────────────────────────────────────────────┬─────────────────────────────────┘
                                              v
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │  ╔══════════════════════════════════════════════════════════════════════╗     │
 │  ║              COMMANDER GATE (AI Trading Robot)                     ║     │
 │  ║  HermesBrain intercepts ALL trade intents                         ║     │
 │  ║  LLM evaluates every trade: APPROVE / REJECT / MODIFY            ║     │
 │  ║  NOT A SINGLE TRADE HAPPENS WITHOUT EXPLICIT APPROVAL             ║     │
 │  ║  Fallback modes: strict | conservative | passthrough              ║     │
 │  ╚══════════════════════════════════════════════════════════════════════╝     │
 └────────────────────────────────────────────┬─────────────────────────────────┘
                                              v
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                   SAFETY GAUNTLET (15 pre-trade checks)                     │
 │  Risk | Policy | Rugpull | Vault | Sandbox | Solver | CircuitBreaker        │
 │  WashTrading | PositionLimits | Concentration | VaR | Stress | Compliance   │
 └────────────────────────────────────────────┬─────────────────────────────────┘
                                              v
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                  PRICE VALIDATION GATE (SAFE/WARN/HALT)                     │
 │  Multi-source price check -> Binary search chunk sizing                     │
 └────────────────────────────────────────────┬─────────────────────────────────┘
                                              v
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                        EXECUTION LAYER                                      │
 │  SmartOrderRouter (5 CEX + 10 DEX) | TWAP | Iceberg | Almgren-Chriss       │
 │  Intent Solver Network (5 competing solvers for best execution)             │
 │  ERC-4626 Vault (standard fund custody) | Flash Loan Arb (zero-capital)    │
 └────────────────────────────────────────────┬─────────────────────────────────┘
                                              v
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │                     POST-TRADE (31 consumers)                               │
 │  PnL | ELO | Memory DAG | Treasury | Marketplace Fees | Agent Payments      │
 │  Portfolio Insurance | Strategy NFT | Social Trading | Observability        │
 │  VaR | Compliance | TCA | Dashboard | Telegram | Voice Response             │
 └──────────────────────────────────────────────────────────────────────────────┘
```

## Platform Stats

| Metric | Value |
|--------|-------|
| Python modules | 122 |
| Lines of code | 48,612 |
| Bus topics | 221 |
| Signal topics | 69 |
| Strategist weighted inputs | 48 |
| Pre-trade safety checks | 15 |
| Post-trade consumers | 31 |
| Supported exchanges | 5 CEX + 10 DEX |
| Supported chains | Ethereum, Base, Solana |
| AI Commander | Every trade requires LLM approval |

## Key Features

### AI Commander (HermesBrain + CommanderGate)
- Every trade intent from every source is intercepted by the AI trading robot
- LLM evaluates each trade: APPROVE, REJECT, or MODIFY
- Supports Ollama (local), OpenAI, Anthropic, Groq, DeepSeek, or any OpenAI-compatible API
- Fallback modes when LLM is unavailable: strict (block all), conservative (high-conviction only), passthrough
- The 120+ agents are the eyes and ears — the AI commander is the brain

### Signal Intelligence (Phases 1-3)
- **Adaptive Kalman Filter** — regime-adaptive noise removal on all signals, posterior covariance as system risk proxy
- **Data Fusion Pipeline** — cross-source convergence scoring with independence weighting (correlated sources discounted)
- **Adversarial Debate Engine** — BullAgent vs BearAgent argue before every trade, ties default to NO TRADE
- **ELO Reputation System** — K=32, bounds 0-1000, agents gain/lose ELO based on trade outcomes
- **Swarm Consensus** — N-agent weighted voting with quorum and supermajority requirements

### Execution Safety (Phases 2, 11)
- **Price Validation Gate** — SAFE/WARN/HALT tiers from multi-source price deviation checks
- **Execution Sandbox** — agents can trade but physically cannot withdraw (TRADE_ONLY permission)
- **Rugpull Detection** — contract age, liquidity depth, holder concentration, honeypot detection
- **15-layer risk gauntlet** — every trade passes size, drawdown, VaR, stress, compliance, factor, policy checks

### DeFi Infrastructure (Phases 4-10)
- **ERC-4626 Vaults** — standard tokenized vault for fund custody, composable with all of DeFi
- **Agent Marketplace** — competitive signal auctions where agents bid, highest-conviction wins
- **Flash Loan Arbitrage** — zero-capital arb via Aave V3 with fork simulation before execution
- **Strategy-as-NFT** — mint, fork, and trade strategies as on-chain NFTs with royalty chains
- **Uniswap v4 Hooks** — concentrated LP management, TWAP routing, dynamic fee monitoring
- **Cross-Chain Coordination** — Circle CCTP for USDC bridging, yield optimization across chains
- **ZK Private Trading** — commit-reveal dark pool with sealed-bid batch matching

### Advanced Trading (Phases 12-20)
- **Prediction Market Trading** — autonomous Polymarket execution with Kelly-sized bets
- **Genetic Strategy Evolution** — population of 50 strategies, crossover + mutation, tournament selection
- **Multi-Model AI Brain** — pluggable adapter for Claude/GPT/Groq/DeepSeek/local models
- **Event Correlation Narrative Engine** — transforms raw events into market stories for AI reasoning
- **Whale Mirror Agent** — auto-copy smart money wallets with signal alignment checks
- **Intent Solver Network** — 5 competing solvers (Uniswap, Aerodrome, SushiSwap, 1inch, cross-DEX)
- **Liquidation Cascade Shield** — health factor monitoring with auto-deleverage before cascade
- **Agent Governance DAO** — ELO-weighted voting on parameter changes, strategy activation, fee structures

### Interfaces (Phases 21-22)
- **Telegram Trading Bot** — /trade, /status, /signals, /risk, /agents, /kill commands
- **Voice-Activated Trading** — "buy 500 dollars of ETH" via Whisper STT or browser Speech API
- **Web Dashboard** — real-time WebSocket streaming, P&L cards, agent tree, Chart.js visualizations
- **Agent Gateway** — HTTP/WebSocket bridge for external AI agents (OpenClaw/Hermes/IronClaw protocols)

### Infrastructure (Phases 23-40)
- **Agent Factory** — dynamic agent spawning based on market conditions, auto-termination on TTL
- **Options Strategy Automation** — covered calls, protective puts, straddles, iron condors, vol arb
- **Social Alpha Scanner v2** — cross-platform virality scoring with coordination (shill) detection
- **Backtesting Arena** — competitive strategy tournaments ranked by risk-adjusted returns
- **Agent Observability Dashboard** — health monitoring, anomaly detection, dependency graph
- **Federated Learning** — agents share model gradients without sharing proprietary data
- **RWA Bridge** — tokenized real-world assets (T-bills, commodities, equities)
- **Autonomous Treasury** — multi-asset allocation, yield optimization, runway tracking
- **MEV Engine** — detect, capture, and redistribute MEV; protect own trades via Flashbots
- **Agent Memory DAG** — long-term learning with semantic retrieval and memory consolidation
- **Sentiment Derivatives** — RSI/MACD on sentiment itself, price-sentiment divergence signals
- **Grid Trading** — arithmetic, geometric, and exponential grid strategies
- **Token Launch Sniper** — early detection + safe entry with rugpull screening
- **Correlation Regime Detector v2** — cross-asset correlation matrix, regime transitions
- **Gas Optimizer** — price forecasting, transaction batching, priority fee optimization
- **Portfolio Insurance** — automated hedging via perp shorts, regime-adaptive coverage
- **Agent Communication Protocol** — structured request/response messaging between agents
- **Swarm Consensus Engine** — multi-agent weighted voting on every trade decision

## Quick Start

```bash
# Clone
git clone https://github.com/EcosystemNetwork/swarmtrader.git
cd swarmtrader

# Install
pip install -e ".[dev]"

# Copy environment config
cp .env.example .env

# Run in mock mode (no API keys needed)
python -m swarmtrader.main mock 120

# Run with web dashboard
python -m swarmtrader.main mock 300 --web --dashboard

# Run with AI Commander (requires LLM)
ollama pull nous-hermes2
python -m swarmtrader.main mock 300 --web --hermes

# Run with real Kraken prices (paper trading)
python -m swarmtrader.main paper 600 --pairs ETHUSD BTCUSD --ws --web

# Run tests
pytest tests/ -v
```

## Docker

```bash
docker compose up --build
# Or:
docker build -t swarmtrader .
docker run -p 8080:8080 --env-file .env swarmtrader mock 300 --web
```

## Trading Modes

| Mode | Prices | Execution | AI Commander | API Keys |
|------|--------|-----------|-------------|----------|
| `mock` | Simulated (GBM) | Dry-run with slippage | Optional | None |
| `paper` | Real (all venues) | Paper trading | Optional | Optional |
| `live` | Real (all venues) | Real orders | Recommended | Required |

When `--hermes` is passed, the AI Commander (CommanderGate) intercepts ALL trade intents. Without it, the rule-based Strategist runs autonomously.

## Commander Mode (AI Trading Robot)

The centerpiece of the platform. When enabled (`--hermes`), the AI trading robot has sole authority over every trade:

```
120+ agents produce signals
         |
    Strategist aggregates (48 weighted inputs)
         |
    Alpha discovery + adversarial debate + consensus voting
         |
  ┌─────────────────────────────────────┐
  │  COMMANDER GATE                     │
  │  AI robot evaluates: APPROVE/REJECT │
  │  No trade without approval          │
  └─────────────────────────────────────┘
         |
    15-layer safety gauntlet
         |
    Price validation gate
         |
    Execution (5 CEX + 10 DEX)
```

Supported LLM providers:
- **Ollama** (local) — gemma3, nous-hermes2, llama3, mistral, qwen2, deepseek-r1
- **OpenAI** — gpt-4o, gpt-4-turbo
- **Anthropic** — claude-opus-4-6, claude-sonnet-4-6
- **Groq** — llama-3.3-70b (fast, free tier)
- **DeepSeek** — deepseek-chat
- **Any OpenAI-compatible** — vLLM, LM Studio, Together, etc.

Configure via `.env`:
```bash
LLM_PROVIDER=ollama          # ollama, openai, anthropic, groq, deepseek, openai-compat
LLM_MODEL=nous-hermes2       # auto-detected if empty
OLLAMA_URL=http://localhost:11434
HERMES_INTERVAL=15            # seconds between LLM calls
COMMANDER_FALLBACK=conservative  # strict, conservative, passthrough
```

## Signal Flow

Every signal passes through multiple intelligence layers before reaching execution:

```
Raw Data (8 sources, 54 subscribers)
  -> 80+ signal agents (59 topics)
    -> Kalman Filter (noise removal)
      -> Data Fusion (convergence scoring)
        -> Strategist (48 weights, 3 regime profiles)
          -> Alpha Hunter (3+ agents agree)
            -> Adversarial Debate (bull vs bear)
              -> Swarm Consensus (N-agent voting)
                -> COMMANDER GATE (LLM approval)
                  -> Safety Gauntlet (15 checks)
                    -> Price Gate (SAFE/WARN/HALT)
                      -> Execution (best venue)
                        -> 31 post-trade consumers
```

## Risk Pipeline (15 Layers)

Every trade intent passes ALL checks before execution:

| # | Check | Description |
|---|-------|-------------|
| 1 | `size_check` | Max single trade size |
| 2 | `allowlist_check` | Only approved asset pairs |
| 3 | `drawdown_check` | Daily loss limit |
| 4 | `rate_limit_check` | Trades per hour cap |
| 5 | `max_positions_check` | Concurrent position limit |
| 6 | `funds_check` | Sufficient wallet balance |
| 7 | `allocation_check` | Per-asset allocation cap |
| 8 | `depth_liquidity_check` | Order book depth |
| 9 | `var_check` | VaR under threshold |
| 10 | `stress_check` | Survives 8 stress scenarios |
| 11 | `compliance_check` | Wash trade + margin checks |
| 12 | `factor_exposure_check` | Factor model limits |
| 13 | `sor_venue_check` | Minimum 2 venue quotes |
| 14 | `agent_policy_check` | Per-agent spending cap |
| 15 | `rugpull_check` | Contract safety screening |

## Multi-Chain Execution

| Chain | Venue | Type | Execution |
|-------|-------|------|-----------|
| — | Kraken | CEX | REST + WS v2 (paper/live) |
| — | Binance, Coinbase, OKX, Bybit | CEX | SOR routing |
| Ethereum/Base | Uniswap v3/v4 | DEX | Swaps via Trading API |
| Multiple | Hyperliquid | Perps DEX | 50x leverage, 180+ symbols |
| Solana | Jupiter | DEX | Aggregated across Raydium, Orca, etc. |
| Multiple | SushiSwap, Aerodrome, Curve, PancakeSwap | DEX | Via SOR |

## Project Structure

```
swarmtrader/                          # 122 modules, 48,612 lines
├── core.py                           # Bus, Signal, TradeIntent, PortfolioTracker
├── main.py                           # Entry point — orchestrates 120+ agents
├── config.py                         # Configuration management
├── database.py                       # Postgres (Neon) + SQLite abstraction
│
├── ── DATA (8 sources) ─────────────────────────────────
├── agents.py, kraken.py, kraken_api.py, kraken_ws.py
├── hyperliquid.py, jupiter.py, birdeye.py, pyth_oracle.py
│
├── ── SIGNALS (80+ agents) ─────────────────────────────
├── strategies.py, agents_advanced.py, ml_signal.py
├── multitf.py, correlation.py, confluence.py
├── whale.py, smart_money.py, onchain.py, feargreed.py
├── social.py, social_agents.py, news.py, polymarket.py
├── liquidation.py, open_interest.py, arbitrage.py
├── feeds.py, signals.py
│
├── ── INTELLIGENCE (Phases 1-3) ────────────────────────
├── kalman.py                         # Adaptive Kalman Filter
├── fusion.py                         # Data Fusion Pipeline
├── debate.py                         # Adversarial Debate + ELO
├── alpha_swarm.py                    # Multi-agent alpha discovery
├── swarm_consensus.py                # N-agent weighted voting
│
├── ── STRATEGY + COMMANDER ─────────────────────────────
├── strategy.py                       # Strategist (48 weights, 3 regimes)
├── hermes_brain.py                   # HermesBrain + CommanderGate
├── nlp_strategy.py                   # Natural language strategy parsing
│
├── ── RISK (15 layers) ─────────────────────────────────
├── risk.py, var.py, stress_test.py, factor_model.py
├── compliance.py, agent_policies.py, safety.py
├── rugpull_detector.py, price_gate.py
│
├── ── EXECUTION ────────────────────────────────────────
├── execution.py, sor.py, microstructure.py, twap.py
├── uniswap.py, arb_executor.py, dex_quotes.py, dex_multi.py
├── exchanges.py, intent_solver.py, flashloan.py
│
├── ── ON-CHAIN (Phases 4-10) ──────────────────────────
├── vault.py                          # ERC-4626 fund custody
├── marketplace.py                    # Competitive agent auctions
├── strategy_nft.py                   # Strategy-as-NFT with royalties
├── v4_hooks.py                       # Uniswap v4 hook integration
├── cross_chain.py                    # Circle CCTP + multi-chain yield
├── zk_trading.py                     # Commit-reveal dark pool
├── erc8004.py, x402_payments.py, lp_manager.py
│
├── ── ADVANCED TRADING (Phases 12-20) ─────────────────
├── prediction_trader.py              # Polymarket autonomous trading
├── strategy_evolution.py             # Genetic algorithm evolution
├── ai_brain.py                       # Multi-model LLM adapter
├── narrative.py                      # Event correlation engine
├── whale_mirror.py                   # Smart money copy trading
├── liquidation_shield.py             # Cascade protection
├── governance.py                     # Agent governance DAO
├── agent_payments.py                 # Micropayment protocol
│
├── ── INTERFACES (Phases 21-22) ───────────────────────
├── telegram_bot.py                   # Telegram trading commands
├── voice_trading.py                  # Voice-activated trading
├── web.py, dashboard.py              # Web dashboard + terminal TUI
├── gateway.py                        # External agent bridge
│
├── ── INFRASTRUCTURE (Phases 23-40) ───────────────────
├── agent_factory.py                  # Dynamic agent spawning
├── options_engine.py                 # Options strategy automation
├── social_alpha_v2.py                # Cross-platform virality scanner
├── backtesting_arena.py              # Competitive tournaments
├── observability.py                  # Agent health monitoring
├── federated.py                      # Federated learning
├── rwa_bridge.py                     # Real-world asset integration
├── treasury.py                       # Autonomous treasury management
├── mev_engine.py                     # MEV detection + protection
├── agent_memory_v2.py                # Memory DAG with semantic retrieval
├── sentiment_derivatives.py          # Sentiment TA indicators
├── grid_trading.py                   # Grid + DCA strategies
├── token_sniper.py                   # Early token detection
├── regime_v2.py                      # Cross-asset correlation regimes
├── gas_optimizer.py                  # Gas forecasting + batching
├── portfolio_insurance.py            # Automated hedging
├── agent_protocol.py                 # Structured inter-agent messaging
│
├── ── PORTFOLIO + LEARNING ────────────────────────────
├── portfolio_opt.py, positions.py, wallet.py, capital_allocator.py
├── agent_learning.py, memory.py, agent_memory_v2.py
├── backtester.py, backtest.py, walkforward.py
│
├── ── SOCIAL ──────────────────────────────────────────
├── social_trading.py                 # Copy trading + revenue sharing
│
└── static/
    └── index.html                    # Web dashboard (Tailwind + Chart.js)
```

## Technology

- **Language**: Python 3.11+ (async/await throughout)
- **Architecture**: Event-driven pub/sub via async `Bus` (221 topics)
- **ML/Math**: All from scratch — no numpy, scipy, sklearn, or ta-lib
- **LLM**: Ollama, OpenAI, Anthropic, Groq, DeepSeek (all optional)
- **CEX**: Kraken, Binance, Coinbase, OKX, Bybit
- **DEX**: Uniswap v3/v4, Jupiter, Hyperliquid, SushiSwap, Aerodrome, Curve, PancakeSwap, Raydium, Orca
- **Blockchain**: ERC-4626, ERC-8004, ERC-7857, x402, Circle CCTP
- **Storage**: Postgres (Neon) primary, SQLite fallback
- **Deployment**: Docker, Railway, Vercel (Neon)
- **Dependencies**: Minimal — `aiohttp`, `python-dotenv`, `web3`, `eth-account`, `psycopg`

## Documentation

| File | Contents |
|------|----------|
| [`README.md`](README.md) | This file — setup, architecture, features |
| [`AGENTS.md`](AGENTS.md) | Complete agent reference (120+ agents) |
| [`SOUL.md`](SOUL.md) | Agent identity, principles, and risk discipline |
| [`ROADMAP.md`](ROADMAP.md) | 20-phase product roadmap with revenue projections |
| [`.env.example`](.env.example) | All environment variables with descriptions |

## License

MIT
