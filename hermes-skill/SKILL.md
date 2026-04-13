---
name: swarmtrader
description: Connect to a SwarmTrader instance as the trading brain. Receive real-time market intelligence from 120+ specialized agents, make buy/sell/hold decisions, and learn from execution results. Supports paper and live trading via Kraken.
version: 1.0.0
author: SwarmTrader
tags: [trading, crypto, defi, swarm-intelligence, market-data, portfolio]
---

# SwarmTrader — AI Trading Brain

Connect Hermes Agent to a running SwarmTrader instance as its **trading brain**. SwarmTrader runs 120+ specialized agents (momentum, RSI, whale tracking, on-chain analysis, social sentiment, funding rates, order flow, etc.) and feeds you a compiled market brief every 15 seconds. You analyze the intelligence and make trading decisions.

## When to Use

- User says "connect to swarmtrader", "start trading", or "trade mode"
- User asks you to manage a crypto portfolio or make trading decisions
- User wants to paper trade or live trade with AI-driven decisions
- User mentions "swarm", "market brief", or "trading brain"

## Architecture

```
120+ SwarmTrader Agents
  (momentum, RSI, whale, sentiment, on-chain, funding, order flow...)
          |
          v
   [Market Brief]  <-- compiled every 15s with all signals
          |
          v
   [YOU — Hermes]  <-- analyze brief, decide buy/sell/hold
          |
          v
   [15 Risk Checks] <-- unanimous consensus required
          |
          v
   [Kraken Executor] <-- paper or live fills
          |
          v
   [Execution Report] <-- PnL, fills, slippage back to you
```

## Prerequisites

1. **SwarmTrader running** with `--hermes --gateway` flags:
   ```bash
   cd /path/to/swarmtrader
   python -m swarmtrader.main paper 3600 --hermes --gateway --web --pairs ETHUSD --capital 500
   ```

2. **Gateway key** — printed at startup, or set via `SWARM_GATEWAY_KEY` env var

3. **Bridge script** running — connects Hermes to SwarmTrader's WebSocket gateway:
   ```bash
   python3 hermes-skill/scripts/swarm_bridge.py \
     --gateway ws://localhost:8080 \
     --key YOUR_GATEWAY_KEY --register
   ```

## How It Works

### 1. You receive a market brief

Every 15 seconds, SwarmTrader compiles all agent signals and sends you a brief like:

```
PRICES: ETH: $3200.50, BTC: $68400.00
REGIME: trending
FEAR/GREED: 72 (Greed)

PORTFOLIO: $500.00 capital | 0 positions | PnL: $0.00

--- ETH SIGNALS (8 agents) ---
CONSENSUS: 6 LONG, 1 SHORT, 1 FLAT
  [momentum] long str=+0.85 conf=0.92 — Golden cross on 1h
  [rsi] long str=+0.72 conf=0.88 — RSI=65, trending up
  [whale] long str=+0.60 conf=0.75 — 3 whale buys >$500k in 10min
  [funding] short str=-0.30 conf=0.65 — Funding rate 0.08% (longs overlevered)
  [sentiment] long str=+0.45 conf=0.70 — Crypto Twitter bullish, Fear/Greed rising
  ...
```

### 2. You make a decision

Respond with a JSON decision:

```json
[{
  "action": "buy",
  "asset": "ETH",
  "size_usd": 50.0,
  "confidence": 0.8,
  "reasoning": "6/8 agents bullish, golden cross confirmed, whale accumulation",
  "order_type": "market",
  "urgency": "medium"
}]
```

Valid actions: `buy`, `sell`, `hold`

### 3. Risk checks protect you

Even if you say "buy", 15 independent risk agents must ALL approve:
- Position size limits ($50 max per trade by default)
- Daily drawdown limit ($100 max loss)
- Rate limiting (max 20 trades/hour)
- Asset allowlist (ETH, BTC, SOL only by default)
- VaR check, stress test, factor exposure limits

If ANY risk check fails, the trade is blocked and you're told why.

### 4. You see the result

After execution (or rejection), you receive a report:
```
FILL: bought 0.0156 ETH @ $3201.10 | fee: $0.05 | slippage: 2.1bps
```
Or:
```
REJECTED: drawdown_check — daily loss $95.50 too close to $100 limit
```

## Decision Format Reference

### Buy/Sell
```json
[{"action": "buy", "asset": "ETH", "size_usd": 50.0, "confidence": 0.8, "reasoning": "why", "order_type": "market"}]
```

### Hold (do nothing)
```json
[{"action": "hold", "reasoning": "Mixed signals, waiting for confirmation"}]
```

### Multiple decisions (rare)
```json
[
  {"action": "sell", "asset": "ETH", "size_usd": 25.0, "confidence": 0.7, "reasoning": "taking profit"},
  {"action": "buy", "asset": "SOL", "size_usd": 30.0, "confidence": 0.6, "reasoning": "rotation"}
]
```

### Fields

| Field | Required | Values | Default |
|-------|----------|--------|---------|
| `action` | yes | `buy`, `sell`, `hold` | — |
| `asset` | for buy/sell | `ETH`, `BTC`, `SOL`, etc. | `ETH` |
| `size_usd` | for buy/sell | 1.0 - max_size | base_size |
| `confidence` | no | 0.0 - 1.0 | 0.5 |
| `reasoning` | no | string | "" |
| `order_type` | no | `market`, `limit` | `market` |
| `urgency` | no | `low`, `medium`, `high` | `medium` |

## Trading Principles

When analyzing market briefs, consider:

1. **Consensus matters** — if 6/8 agents agree on direction, that's a strong signal
2. **Confidence weighting** — a 0.92 confidence momentum signal matters more than a 0.45 sentiment signal
3. **Regime awareness** — in trending markets, favor momentum; in ranging markets, favor mean reversion
4. **Risk first** — never go all-in; use 5-15% of capital per trade
5. **Funding rates** — extreme positive funding = too many longs = contrarian short signal
6. **Whale activity** — large wallet movements often precede price moves
7. **Don't overtrade** — "hold" is a valid and often correct decision
8. **Cut losers fast** — if a position is down and signals flip, sell

## Memory Integration

Save what you learn to your memory:
- Which signal combinations led to winning trades
- Which market regimes you perform best in
- Your optimal position sizing for different confidence levels
- Patterns where risk checks correctly blocked bad trades

Over time, your decisions should improve as you build pattern recognition from real execution feedback.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| No briefs arriving | Check SwarmTrader is running with `--hermes --gateway` |
| "Gateway WS not connected" | Bridge script not running or wrong key |
| All trades rejected | Risk checks working — check if capital/drawdown limits are too tight |
| "rate limit exceeded" | You're deciding too fast — hold more often |

## Files

- `scripts/swarm_bridge.py` — WebSocket bridge that connects Hermes to SwarmTrader
- `references/protocol.md` — Full WebSocket protocol specification
- `references/signals.md` — All 120+ agent signal types explained
