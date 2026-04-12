# SwarmTrader Agent Signals

Signals appear in market briefs as:
```
[agent_id] direction str=+/-strength conf=confidence — rationale
```

## Core Technical Agents

| Agent | Signal | What it measures |
|-------|--------|-----------------|
| `momentum` | long/short | SMA crossover (fast/slow), trend strength |
| `mean_rev` | long/short | Bollinger Band deviation, mean reversion setup |
| `rsi` | long/short | RSI overbought (>70) / oversold (<30) |
| `macd` | long/short | MACD line vs signal line crossover |
| `bollinger` | long/short | Price vs upper/lower Bollinger bands |
| `vwap` | long/short | Price relative to volume-weighted average price |
| `ichimoku` | long/short | Cloud breakout, Tenkan/Kijun cross |
| `kalman` | long/short | Kalman-filtered trend direction |

## On-Chain / DeFi Agents

| Agent | Signal | What it measures |
|-------|--------|-----------------|
| `whale` | long/short | Large wallet movements (>$500k) |
| `smart_money` | long/short | Known profitable wallet activity |
| `onchain` | long/short | Active addresses, gas usage, TVL changes |
| `funding_rate` | long/short | Perp funding rate extremes (mean reversion) |
| `orderbook` | long/short | Order book imbalance, bid/ask pressure |
| `hyperliquid` | long/short | HL order flow, liquidations, OI changes |

## Sentiment / External

| Agent | Signal | What it measures |
|-------|--------|-----------------|
| `news` | long/short | News article sentiment (crowd-voted) |
| `sentiment` | long/short | Social media / Crypto Twitter sentiment |
| `fear_greed` | long/short | Fear & Greed Index (0-100) |
| `social` | long/short | Social volume spikes, trending mentions |

## Meta / Ensemble

| Agent | Signal | What it measures |
|-------|--------|-----------------|
| `confluence` | long/short | Multi-signal agreement (3+ agents aligned) |
| `debate` | long/short | Bull vs bear debate scoring |
| `fusion` | long/short | ML-weighted signal combination |
| `correlation` | long/short | Cross-asset correlation breaks |
| `arbitrage` | long/short | Cross-venue price discrepancies |

## Reading Signal Strength

- `str=+0.85` — strong bullish signal (range: -1.0 to +1.0)
- `conf=0.92` — 92% confidence in this signal
- **Effective weight** = strength x confidence
- Signals with conf < 0.3 are noise — ignore them
- 6/8 agents agreeing = strong consensus, 4/8 = mixed, act cautiously
