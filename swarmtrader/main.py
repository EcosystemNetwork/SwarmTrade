"""Run: python -m swarmtrader.main [seconds]"""
from __future__ import annotations
import asyncio, logging, sys
from pathlib import Path
from . import (Bus, MockScout, MomentumAnalyst, MeanReversionAnalyst, VolatilityAnalyst,
               Strategist, RiskAgent, Coordinator, Simulator, Executor, Auditor,
               size_check, allowlist_check, drawdown_check)


async def run(duration: float = 30.0, db: Path = Path("swarm.db"),
              kill_switch: Path = Path("KILL")):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    bus = Bus()
    state: dict = {"daily_pnl": 0.0}

    # data + analysts
    scout = MockScout(bus)
    MomentumAnalyst(bus); MeanReversionAnalyst(bus); VolatilityAnalyst(bus)

    # strategy
    Strategist(bus, base_size=1000.0)

    # risk: 3 agents
    risks = [
        RiskAgent(bus, "size", size_check(max_size=5000.0)),
        RiskAgent(bus, "allowlist", allowlist_check({"ETH", "USDC"})),
        RiskAgent(bus, "drawdown", drawdown_check(state, max_dd=50.0)),
    ]
    Coordinator(bus, n_risk_agents=len(risks))

    # exec pipeline
    Simulator(bus)
    Executor(bus, kill_switch=kill_switch, dry_run=True)
    Auditor(bus, db_path=db, state=state)

    scout_task = asyncio.create_task(scout.run())
    try:
        await asyncio.sleep(duration)
    finally:
        scout.stop()
        scout_task.cancel()
        try: await scout_task
        except asyncio.CancelledError: pass

    logging.getLogger("swarm").info("FINAL daily_pnl=%+.4f", state["daily_pnl"])


if __name__ == "__main__":
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    asyncio.run(run(secs))
