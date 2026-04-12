# Swarm Trade

Multi-agent DeFi trading swarm. Reference implementation — **dry-run only** out of the box.

## Architecture

```
MockScout ──► market.snapshot ──┬─► MomentumAnalyst ──► signal.momentum ─┐
                                ├─► MeanReversionAnalyst ► signal.mean_rev┤
                                └─► VolatilityAnalyst ──► signal.vol ─────┤
                                                                          ▼
                                                                    Strategist
                                                                          │ intent.new
                                ┌─────────────────────────────────────────┤
                                ▼                                         ▼
                       RiskAgent x3 ──► risk.verdict ──► Coordinator ──► exec.go
                                                                          │
                                                                  Simulator (sets min_out)
                                                                          │ exec.simulated
                                                                          ▼
                                                                       Executor
                                                                          │ exec.report + audit.attribution
                                                                          ▼
                                                                Auditor (SQLite) ──► Strategist (adaptive weights)
```

No single agent can move funds. Execution requires: weighted strategist score above threshold, **all** risk agents approve, simulator returns a quote, intent TTL valid, kill-switch file absent.

## Run

```
python -m swarmtrader.main 30        # 30 seconds, dry-run
touch KILL                            # emergency stop (file presence blocks all execs)
sqlite3 swarm.db 'select * from reports;'
```

## Going live (do not skip any of these)

1. Replace `MockScout` with real RPC/subgraph data.
2. Implement `Simulator` against a forked node (Anvil/Tenderly) or on-chain quoter.
3. Implement `Executor._submit` with `web3.py` + a private relay (Flashbots / MEV-Share).
4. Move signing to KMS or a hardware wallet. Never keep raw keys in process memory.
5. Audit, paper-trade for weeks, then start with capital you can lose.

This is research code. Not financial advice.
