# SwarmTrader

**Multi-Agent Autonomous Trading Platform** — A swarm of specialized AI agents that collaborate to analyze markets, generate signals, manage risk, and execute trades autonomously via Kraken CLI.

Built for the [AI Trading Agents Hackathon](https://lablab.ai) (March 30 – April 12, 2026).

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA LAYER                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ KrakenScout  │  │  OrderBook   │  │   Funding    │          │
│  │ (REST/WS)    │  │   Agent      │  │  Rate Agent  │          │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘          │
│         │                 │                 │                   │
│  ┌──────┴───────┐  ┌──────┴───────┐  ┌──────┴───────┐          │
│  │ SpreadAgent  │  │  PRISM/Strykr│  │ Multi-Asset  │          │
│  │ (liquidity)  │  │  AI Signals  │  │ (ETH/BTC/SOL)│          │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘          │
└─────────┼─────────────────┼─────────────────┼───────────────────┘
          │    market.snapshot / signal.*      │
          ▼                 ▼                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                      ANALYSIS LAYER                             │
│  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐  │
│  │ Momentum   │ │ Mean Rev   │ │ Volatility │ │  Regime    │  │
│  │ (20-window)│ │ (50-window)│ │ (30-window)│ │ (ADX+Hurst)│  │
│  └─────┬──────┘ └─────┬──────┘ └─────┬──────┘ └─────┬──────┘  │
└────────┼──────────────┼──────────────┼──────────────┼──────────┘
         │   signal.momentum/mean_rev/vol/regime      │
         ▼              ▼              ▼              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     STRATEGY LAYER                              │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                    STRATEGIST                             │  │
│  │  • Regime-aware adaptive weight adjustment                │  │
│  │  • Kelly criterion position sizing                        │  │
│  │  • Volatility & spread damping                            │  │
│  │  • PnL-based attribution learning                         │  │
│  └──────────────────────┬───────────────────────────────────┘  │
│                         │ intent.new                           │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐                       │
│  │ Size     │ │ Allowlist│ │ Drawdown │  ← Risk Agents         │
│  │ Check    │ │ Check    │ │ Check    │                        │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘                       │
│       └─────── Coordinator (quorum) ──────┘                    │
│                     │ exec.go (all must approve)               │
└─────────────────────┼──────────────────────────────────────────┘
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                    EXECUTION LAYER                              │
│  ┌────────────┐  ┌────────────────┐  ┌──────────────────────┐ │
│  │ Simulator  │→ │ KrakenExecutor │→ │ Auditor (SQLite)     │ │
│  │ (slippage) │  │ (paper/live)   │  │ + PnL tracking       │ │
│  └────────────┘  └────────────────┘  └──────────────────────┘ │
│                                                                │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │              CIRCUIT BREAKER SYSTEM                       │ │
│  │  • Consecutive loss detection    • Max drawdown halt      │ │
│  │  • Volatility spike pause        • Dead man's switch      │ │
│  │  • Position flattener            • Kill switch file       │ │
│  └──────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

## Features

### 9 Specialized Agents
| Agent | Type | Signal |
|-------|------|--------|
| **KrakenScout** | Data | Real-time prices via REST or WebSocket |
| **MomentumAnalyst** | Signal | 20-period return-based trend detection |
| **MeanReversionAnalyst** | Signal | 50-period z-score mean reversion |
| **VolatilityAnalyst** | Signal | 30-period vol as confidence damper |
| **OrderBookAgent** | Signal | L2 bid/ask imbalance (top-of-book weighted) |
| **FundingRateAgent** | Signal | Futures funding rate contrarian signals |
| **SpreadAgent** | Signal | Bid-ask spread liquidity monitoring |
| **RegimeAgent** | Signal | ADX + Hurst exponent regime classification |
| **PRISMSignalAgent** | Signal | External AI signals (momentum, breakouts, volume) |

### Intelligent Strategy
- **Regime-aware weighting**: Automatically shifts strategy between trending/mean-reverting/volatile profiles
- **Kelly criterion sizing**: Mathematically optimal position sizing based on win rate and payoff ratio
- **Adaptive learning**: Weights evolve toward agents whose signals predict profitable trades
- **Multi-signal fusion**: Combines 6+ signal sources with normalized confidence scoring

### Safety & Risk Management
- **Multi-agent risk quorum**: ALL risk agents (size, allowlist, drawdown, rate limit) must unanimously approve before any trade executes
- **Circuit breakers**: Consecutive loss detection, drawdown limits, volatility spike halt with automatic cooldown
- **Rate limiter**: Max trades per hour to prevent overtrading in choppy markets
- **Dead man's switch**: Auto-cancels all orders if agent becomes unresponsive
- **Position flattener**: Emergency close of all positions on circuit breaker trigger
- **Daily drawdown reset**: Calendar-day automatic reset of loss tracking
- **Kill switch**: `touch KILL` to instantly halt all trading

### Web Dashboard
Real-time monitoring dashboard served via aiohttp with WebSocket streaming:
- Live agent activity, signal strength, and trade flow
- Interactive kill switch toggle
- Trade history query from SQLite audit log
- Start with `--web` flag

### Multi-Asset Trading
Trade ETH, BTC, SOL, and 650+ pairs simultaneously with independent signal generation per asset.

### Backtesting Engine
Replay historical Kraken OHLC data through the full agent pipeline with simulated clock, computing Sharpe ratio, max drawdown, win rate, and per-trade PnL.

## Quick Start

```bash
# Install dependencies
pip install aiohttp python-dotenv

# Install Kraken CLI
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh

# Set up credentials
cp .env.example .env
# Edit .env with your Kraken API key and private key

# Initialize paper trading
kraken paper init --balance 10000

# Run in mock mode (no keys needed)
python -m swarmtrader.main mock 60

# Run with real Kraken data + paper trading
python -m swarmtrader.main paper 300 --pairs ETHUSD

# Multi-asset with dashboard
python -m swarmtrader.main paper 600 --pairs ETHUSD BTCUSD SOLUSD --dashboard

# Run backtester
python -m swarmtrader.backtest --pair ETHUSD --interval 5 --base-size 500

# Run tests
python -m tests.test_smoke
```

## Modes

| Mode | Data Source | Execution | Keys Required |
|------|------------|-----------|---------------|
| `mock` | Simulated (GBM) | Dry-run | None |
| `paper` | Real Kraken | Paper trades | None |
| `live` | Real Kraken | Real orders | API key + secret |

## CLI Options

```
python -m swarmtrader.main [mock|paper|live] [duration_seconds]
  --pairs ETHUSD BTCUSD SOLUSD    Trading pairs
  --base-size 500                  Base trade size in USD
  --max-size 2000                  Max single trade size
  --max-drawdown 200               Max daily drawdown (USD)
  --ws                             Use WebSocket streaming
  --poll-interval 2.0              REST poll interval (seconds)
  --dashboard                      Live terminal dashboard
  --web                            Launch web dashboard (http://localhost:8080)
  --web-port 8080                  Web dashboard port
  --no-advanced                    Disable advanced agents
```

## Technology

- **Kraken CLI** — Zero-dependency Rust binary for market data and trade execution
- **PRISM/Strykr API** — AI-powered trading signals and risk metrics
- **Python asyncio** — Event-driven pub/sub architecture
- **SQLite** — Append-only audit trail with PnL tracking

## Project Structure

```
swarmtrader/
├── core.py              # Data types + async pub/sub bus
├── agents.py            # Core analysts (momentum, mean reversion, volatility)
├── agents_advanced.py   # Order book, funding rate, spread, regime detection
├── strategy.py          # Strategist + risk agents + coordinator
├── execution.py         # Simulator + executor + auditor
├── kraken.py            # Kraken CLI integration (scout + executor)
├── signals.py           # PRISM/Strykr AI signal integration
├── safety.py            # Circuit breakers + dead man's switch
├── risk.py              # Rate limiter, position tracker, daily drawdown
├── dashboard.py         # Live terminal dashboard
├── web.py               # Web dashboard (aiohttp + WebSocket)
├── backtest.py          # Historical backtesting engine
└── main.py              # Entry point with mode selection
```

## Going Live Checklist

1. [ ] Generate Kraken API keys with trade permissions
2. [ ] Store keys in `.env` (never commit!)
3. [ ] Run paper mode for 24+ hours, verify PnL tracking
4. [ ] Review circuit breaker thresholds
5. [ ] Set `--max-drawdown` to your risk tolerance
6. [ ] Switch to `live` mode
7. [ ] Monitor with `--dashboard`
8. [ ] Keep `touch KILL` ready as emergency stop

## License

MIT
