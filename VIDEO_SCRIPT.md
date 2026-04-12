# SwarmTrader Video Presentation Script

**Target: 3-5 minutes | Screen recording + voiceover**

---

## INTRO (30s)

**Show:** Title slide from `/slides` (Slide 1)

> "This is SwarmTrader — a multi-agent autonomous trading platform where 9 specialized AI agents collaborate in real-time to analyze markets, manage risk, and execute trades through Kraken CLI."
>
> "Unlike single-model trading bots, SwarmTrader uses a swarm intelligence approach — each agent is an expert in one domain, and they must reach consensus before any trade executes."

---

## THE PROBLEM (30s)

**Show:** Slide 2 (The Problem)

> "Most autonomous trading agents suffer from three critical flaws:
> 1. Single-agent bias — one model, one perspective, blind to regime shifts
> 2. No risk guardrails — one bad trade cascades into disaster
> 3. Black box execution — no audit trail, no attribution, no transparency
>
> SwarmTrader solves all three."

---

## ARCHITECTURE WALKTHROUGH (60s)

**Show:** Slide 3 (The Swarm architecture)

> "Here's how the swarm works:
>
> **Data Layer** — KrakenScout pulls real-time prices via REST or WebSocket. OrderBookAgent analyzes L2 depth imbalance. FundingRateAgent detects contrarian signals from futures funding. PRISM AI adds external sentiment and breakout signals.
>
> **Analysis Layer** — Four analysts process market data independently: Momentum tracks trends, MeanReversion finds z-score deviations, Volatility dampens confidence, and RegimeAgent uses ADX plus Hurst exponent to classify the market regime.
>
> **Strategy Layer** — The Strategist fuses all signals with regime-aware adaptive weights and sizes positions using the Kelly criterion. Then every trade must pass through a 4-agent risk quorum — size check, allowlist, drawdown limit, and rate limiter. ALL must approve, or the trade is blocked.
>
> **Execution Layer** — Trades route through a slippage simulator, then execute via Kraken CLI in paper or live mode. The Auditor logs everything to SQLite."

---

## RISK MANAGEMENT (45s)

**Show:** Slide 5 (8 Layers of Risk)

> "Risk management is our strongest feature — 8 independent layers:
>
> Multi-agent quorum with unanimous approval. Circuit breakers that halt after 5 consecutive losses OR when drawdown exceeds the limit. Rate limiter capping trades per hour. A kill switch — just touch a file or click the button. A dead man's switch that auto-cancels all orders if the agent goes unresponsive. And a position flattener that emergency-closes everything on circuit breaker activation.
>
> Every single trade has a full audit trail with PnL attribution back to the individual agents that supported it."

---

## LIVE DEMO (60s)

**Show:** Terminal + Web Dashboard side by side

> "Let me show it running. I'll start a paper trading session on Kraken with the web dashboard."

**Run:**
```bash
python -m swarmtrader.main paper 120 --pairs ETHUSD --web
```

**Show in browser:** `http://localhost:8080`

> "On the left, you can see the agent swarm — each agent lights up as it processes data. The center shows the live candlestick chart and trade activity. On the right, signal analysis shows real-time strength and confidence from each agent, and the risk consensus panel shows the quorum for each trade.
>
> Watch — there's an intent coming in... the strategist detected a momentum signal aligned with the orderbook imbalance... all 4 risk agents approved... and it's filled via Kraken paper trading.
>
> The kill switch button at the top right instantly halts everything."

---

## PERFORMANCE REPORT (30s)

**Show:** Open `/report` in browser

> "After any session, we generate a full performance report — Sharpe ratio, Sortino ratio, max drawdown, profit factor, equity curve, and a PnL distribution histogram. Every trade is logged with its agent attribution. This is also available as JSON via the API."

---

## CLOSING (30s)

**Show:** Slide 9 (Thank You)

> "SwarmTrader: 9 agents, 8 risk layers, 3 market regimes, zero black boxes. Built with just 2 Python dependencies on top of Kraken CLI.
>
> Check out the GitHub repo and try it yourself — mock mode requires zero API keys."

---

## Recording Tips

1. **Screen resolution:** 1920x1080, browser zoom 100%
2. **Terminal:** Use a dark theme, font size 14+
3. **Browser tabs:** Pre-open dashboard, report page, and slides
4. **Paper trading:** Run `kraken paper init --balance 10000` before recording
5. **Timing:** Practice the demo section — trades come in every 2-5 seconds in mock mode
6. **Backup plan:** If paper trading is slow, switch to `mock 120 --web` for guaranteed activity
