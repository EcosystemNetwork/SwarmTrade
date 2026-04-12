# SwarmTrader Video Presentation Script

**Target: 3-5 minutes | Screen recording + voiceover**

---

## INTRO (30s)

**Show:** Title slide

> "This is SwarmTrader — a multi-agent autonomous trading platform where 120+ specialized AI agents collaborate in real-time to analyze markets, manage risk, and execute trades across 5 CEXs and 10 DEXs on 3 chains."
>
> "Unlike single-model trading bots, SwarmTrader uses a swarm intelligence approach — each agent is an expert in one domain, and they must reach consensus through a 15-layer risk pipeline before any trade executes."

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
> **Data Layer** — KrakenScout pulls real-time prices via REST or WebSocket v2. HyperliquidAgent streams L2 orderbook depth and funding rates. JupiterPriceScout aggregates Solana DEX quotes. BirdEyeAgent adds token analytics. PythOracle provides decentralized price redundancy. And 6 DEX quoters scan SushiSwap, Aerodrome, Curve, PancakeSwap, Raydium, and Orca.
>
> **Analysis Layer** — 30+ signal agents process data independently: Momentum, MeanReversion, Volatility for core analysis. RSI, MACD, Bollinger, VWAP, Ichimoku for technicals. OrderBook, Funding, Spread, and Regime for advanced market structure. SmartMoney for whale wallet shadowing. Polymarket for prediction market signals. And an ML agent with gradient-boosted decision trees — all pure stdlib, no numpy.
>
> **Strategy Layer** — The Strategist fuses all signals with regime-aware adaptive weights and sizes positions using the Kelly criterion. Or optionally, the Hermes LLM brain (local Ollama) takes over, reading all 120+ agent signals and making AI-powered decisions. Then every trade must pass through a 15-layer risk pipeline — size, drawdown, VaR, stress test, compliance, factor exposure, agent spending policies — ALL must approve, or the trade is blocked.
>
> **Execution Layer** — Trades route through a Smart Order Router comparing quotes across 5 CEXs and 10 DEXs. Execution via KrakenExecutor, HyperliquidExecutor, JupiterExecutor, or UniswapExecutor. Plus TWAP, Iceberg, and cross-venue arbitrage execution."

---

## RISK MANAGEMENT (45s)

**Show:** Slide 5 (15 Layers of Risk)

> "Risk management is our strongest feature — 15 independent layers:
>
> Size limits, asset allowlists, daily drawdown tracking, trade rate limiting, position count caps, sufficient funds verification, per-asset allocation limits, order book depth checks, Value-at-Risk with three methods — historical, parametric, and Monte Carlo — stress testing against 8 historical scenarios, wash trade detection plus margin monitoring, factor model exposure limits, portfolio rebalance triggers, smart order routing venue requirements, and per-agent spending cap enforcement.
>
> Plus: circuit breakers that halt after 5 consecutive losses or drawdown exceeds the limit. A kill switch — just touch a file or click the button. Hardware wallet signing with configurable USD thresholds. And a position flattener that emergency-closes everything on circuit breaker activation."

---

## LIVE DEMO (60s)

**Show:** Terminal + Web Dashboard side by side

> "Let me show it running. I'll start a session with the web dashboard."

**Run:**
```bash
python -m swarmtrader.main mock 120 --web --dashboard --pairs ETHUSD BTCUSD
```

**Show in browser:** `http://localhost:8080`

> "On the left, you can see the agent swarm — each agent lights up as it processes data. The center shows the live candlestick chart and trade activity. On the right, signal analysis shows real-time strength and confidence from each agent, and the risk consensus panel shows the 15-layer pipeline for each trade.
>
> Watch — there's an intent coming in... the strategist detected a momentum signal aligned with the orderbook imbalance... all 15 risk layers approved... and it's executed through the smart order router.
>
> The social trading panel shows the agent leaderboard — which agents are producing profitable signals. The alpha swarm panel shows the 4-agent opportunity detection pipeline.
>
> The kill switch button at the top right instantly halts everything."

---

## HERMES LLM BRAIN (30s)

**Show:** Terminal with `--hermes` flag

> "What makes this truly autonomous — the Hermes LLM brain. When you add the `--hermes` flag, a local Ollama model replaces the rule-based strategy. It reads the full context — all 120+ agent signals, portfolio state, risk metrics, market regime — and generates trade decisions. The rule-based Strategist runs as a backup. All risk checks still apply."

---

## PERFORMANCE REPORT (30s)

**Show:** Open `/report` in browser

> "After any session, we generate a full performance report — Sharpe ratio, Sortino ratio, max drawdown, profit factor, equity curve, and a PnL distribution histogram. Every trade is logged with its agent attribution. The backtester validates 10 strategies with walk-forward and Monte Carlo simulation."

---

## CLOSING (30s)

**Show:** Slide 9 (Thank You)

> "SwarmTrader: 120+ agents, 15 risk layers, 5 CEXs, 10 DEXs, 3 chains, zero black boxes. Multi-agent consensus with optional LLM brain. Built in pure Python with just 6 dependencies.
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
7. **Hermes demo:** Pull model first: `ollama pull nous-hermes2`
