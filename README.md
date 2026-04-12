# SwarmTrader

**Multi-Agent Autonomous Trading Platform** -- A swarm of 9 specialized AI agents that collaborate in real-time to analyze markets, generate signals, manage risk, and execute trades autonomously via Kraken CLI.

Built for the [AI Trading Agents Hackathon](https://lablab.ai) (March 30 - April 12, 2026).

## Architecture

```
                          SWARMTRADER AGENT PIPELINE
 ================================================================

 DATA LAYER                    ANALYSIS LAYER
 +----------------+            +-------------------+
 | KrakenScout    |  prices    | Momentum (20w)    |  signal.*
 | (REST / WS)    |----------->| MeanReversion(50w)|----------+
 +----------------+            | Volatility (30w)  |          |
 | OrderBookAgent |            | RegimeAgent       |          |
 | FundingRate    |            | (ADX + Hurst)     |          |
 | SpreadAgent    |            +-------------------+          |
 | PRISM AI       |                                           |
 +----------------+                                           v
                              STRATEGY LAYER
                              +------------------------------------+
                              | STRATEGIST                         |
                              | - Regime-aware adaptive weights    |
                              | - Kelly criterion position sizing  |
                              | - PnL attribution learning         |
                              +------------------+-----------------+
                                                 | intent.new
                              +------------------v-----------------+
                              | RISK QUORUM (all must approve)     |
                              | [Size] [Allowlist] [Drawdown] [Rate]|
                              +------------------+-----------------+
                                                 | exec.go
                              EXECUTION LAYER    v
                              +------------------------------------+
                              | Simulator -> KrakenExecutor        |
                              | (slippage)   (paper / live)        |
                              +------------------------------------+
                              | CircuitBreaker | PositionFlattener |
                              | Kill Switch    | Dead Man's Switch |
                              | Auditor (SQLite append-only)       |
                              +------------------------------------+
```

## Key Features

### 9 Specialized Agents

| Agent | Type | Signal |
|-------|------|--------|
| **KrakenScout** | Data | Real-time prices via REST or WebSocket |
| **OrderBookAgent** | Data | L2 bid/ask imbalance (top-of-book weighted) |
| **FundingRateAgent** | Data | Futures funding rate contrarian signals |
| **SpreadAgent** | Data | Bid-ask spread liquidity monitoring |
| **MomentumAnalyst** | Analysis | 20-period return-based trend detection |
| **MeanReversionAnalyst** | Analysis | 50-period z-score mean reversion |
| **VolatilityAnalyst** | Analysis | 30-period vol as confidence damper |
| **RegimeAgent** | Analysis | ADX + Hurst exponent regime classification |
| **PRISMSignalAgent** | External | AI signals (sentiment, volume spikes, breakouts) |

### Intelligent Strategy

- **Regime-aware weighting**: Detects trending / mean-reverting / volatile markets and shifts agent weights automatically
- **Kelly criterion sizing**: Mathematically optimal position sizing based on win rate and payoff ratio (half-Kelly for safety)
- **Adaptive learning**: Weights evolve toward agents whose signals predict profitable trades (+5% boost / -3% penalty)
- **Multi-signal fusion**: 6 signal sources with normalized confidence scoring and vol/spread damping

### 8 Layers of Risk Management

1. **Multi-agent risk quorum** -- ALL 4 risk agents (size, allowlist, drawdown, rate limit) must unanimously approve before any trade executes
2. **Circuit breakers** -- Consecutive loss detection (5), drawdown limits, volatility spike halt with automatic cooldown
3. **Rate limiter** -- Max trades per hour to prevent overtrading in choppy markets
4. **Daily drawdown limit** -- Configurable max daily loss with midnight UTC auto-reset
5. **Kill switch** -- `touch KILL` to instantly halt all trading, or one-click from web dashboard
6. **Dead man's switch** -- Auto-cancels all orders via Kraken CLI if agent becomes unresponsive
7. **Position flattener** -- Emergency close of all positions on circuit breaker trigger
8. **Full audit trail** -- Every intent, verdict, and execution logged to SQLite with per-agent PnL attribution

### Performance Reporting

- **Sharpe ratio**, **Sortino ratio**, **Calmar ratio**
- **Equity curve** with drawdown visualization
- **PnL distribution** histogram
- **Per-trade log** with agent attribution
- **HTML export**: `python -m swarmtrader.report --html report.html`
- **JSON API**: `GET /api/report` from web dashboard

### Web Dashboard

Real-time command center served via aiohttp with WebSocket streaming:
- Live agent activity panel with status indicators
- Signal strength bars with weighted score display
- Risk consensus panel with per-intent verdict tracking
- Candlestick price chart (canvas-rendered)
- Active intents with TTL countdown + execution log
- One-click kill switch
- Performance report at `/report`
- Presentation slides at `/slides`

### Backtesting Engine

Replay historical Kraken OHLC data through the full agent pipeline:
```bash
python -m swarmtrader.backtest --pair ETHUSD --interval 5 --base-size 500
```
Computes Sharpe ratio, max drawdown, win rate, and per-trade PnL with simulated clock for accurate cooldown/TTL behavior.

### Multi-Asset Trading

Trade ETH, BTC, SOL, XRP, ADA, DOT, LINK, AVAX, and 650+ pairs simultaneously with independent signal generation per asset.

## Quick Start

```bash
# Install dependencies (just 2 packages!)
pip install aiohttp python-dotenv

# Install Kraken CLI
curl --proto '=https' --tlsv1.2 -LsSf \
  https://github.com/krakenfx/kraken-cli/releases/latest/download/kraken-cli-installer.sh | sh

# Set up credentials (optional for paper mode)
cp .env.example .env
# Edit .env with your Kraken API key and private key

# Initialize paper trading
kraken paper init --balance 10000

# Run in mock mode (no keys needed)
python -m swarmtrader.main mock 60

# Run with real Kraken data + paper trading + web dashboard
python -m swarmtrader.main paper 300 --pairs ETHUSD --web

# Multi-asset with terminal dashboard
python -m swarmtrader.main paper 600 --pairs ETHUSD BTCUSD SOLUSD --dashboard

# Run backtester
python -m swarmtrader.backtest --pair ETHUSD --interval 5 --base-size 500

# Generate performance report
python -m swarmtrader.report --db swarm.db --html report.html

# Run tests
python -m pytest tests/
```

## Modes

| Mode | Data Source | Execution | Keys Required |
|------|------------|-----------|---------------|
| `mock` | Simulated (GBM) | Dry-run | None |
| `paper` | Real Kraken | Paper trades via Kraken CLI | None |
| `live` | Real Kraken | Real market orders | API key + secret |

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

| Component | Technology |
|-----------|-----------|
| **Runtime** | Python 3.11+ asyncio |
| **Market Data** | Kraken CLI (zero-dep Rust binary) |
| **AI Signals** | PRISM / Strykr API |
| **Web Dashboard** | aiohttp + WebSocket + Canvas |
| **Audit Trail** | SQLite (append-only) |
| **Dependencies** | Just 2: `aiohttp` + `python-dotenv` |

## Project Structure

```
swarmtrader/
  core.py              # Data types + async pub/sub bus
  agents.py            # Core analysts (momentum, mean reversion, volatility)
  agents_advanced.py   # Order book, funding rate, spread, regime detection
  strategy.py          # Strategist + risk agents + coordinator
  execution.py         # Simulator + executor + auditor
  kraken.py            # Kraken CLI integration (scout + executor)
  signals.py           # PRISM/Strykr AI signal integration
  safety.py            # Circuit breakers + dead man's switch
  risk.py              # Rate limiter, position tracker, daily drawdown
  report.py            # Performance report generator (Sharpe, Sortino, equity curve)
  backtest.py          # Historical backtesting engine
  dashboard.py         # Live terminal dashboard
  web.py               # Web dashboard (aiohttp + WebSocket)
  static/
    index.html         # Command center UI
    slides.html        # Hackathon presentation deck
tests/
  test_smoke.py        # Integration tests
```

## Hackathon Prize Targets

| Prize | Our Edge |
|-------|---------|
| **Best Risk-Adjusted Return ($5k)** | 8-layer risk management, Kelly criterion sizing, regime-aware adaptive strategy |
| **Best Compliance & Risk Guardrails** | Multi-agent quorum, circuit breakers, dead man's switch, full audit trail, position flattener |
| **Kraken Challenge** | Deep Kraken CLI integration: REST + WS data, paper/live execution, orderbook depth, funding rates, emergency cancel-all |

## License

MIT
