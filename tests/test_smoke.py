"""Smoke tests. Run: python -m tests.test_smoke"""
import asyncio, tempfile, argparse
from pathlib import Path
from swarmtrader.main import run
from swarmtrader import Bus, TradeIntent, Coordinator, RiskAgent, size_check, allowlist_check


def _mock_args(db: Path, kill: Path, duration: float = 6.0) -> argparse.Namespace:
    return argparse.Namespace(
        mode="mock", duration=duration, pairs=["ETHUSD"],
        base_size=1000.0, max_size=5000.0, max_drawdown=50.0,
        db=str(db), kill_switch=str(kill), ws=False, poll_interval=2.0,
        dashboard=False, no_advanced=True,
    )


async def test_full_pipeline():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        kill = Path(d) / "KILL"
        await run(_mock_args(db, kill, duration=6.0))
        assert db.exists()
    print("OK full_pipeline")


async def test_veto_blocks():
    bus = Bus()
    fired = []
    async def capture(i): fired.append(i)
    bus.subscribe("exec.go", capture)
    RiskAgent(bus, "size", size_check(100.0))      # will reject
    RiskAgent(bus, "allow", allowlist_check({"ETH","USDC"}))
    Coordinator(bus, n_risk_agents=2)
    intent = TradeIntent.new(asset_in="USDC", asset_out="ETH",
                             amount_in=500.0, min_out=0, ttl=9e9)
    await bus.publish("intent.new", intent)
    await asyncio.sleep(0.2)
    assert not fired, "veto should block exec.go"
    print("OK veto_blocks")


async def test_quorum_passes():
    bus = Bus()
    fired = []
    async def capture(i): fired.append(i)
    bus.subscribe("exec.go", capture)
    RiskAgent(bus, "size", size_check(10_000.0))
    RiskAgent(bus, "allow", allowlist_check({"ETH","USDC"}))
    Coordinator(bus, n_risk_agents=2)
    intent = TradeIntent.new(asset_in="USDC", asset_out="ETH",
                             amount_in=500.0, min_out=0, ttl=9e9)
    await bus.publish("intent.new", intent)
    await asyncio.sleep(0.2)
    assert len(fired) == 1, f"expected 1 exec.go, got {len(fired)}"
    print("OK quorum_passes")


async def main():
    await test_veto_blocks()
    await test_quorum_passes()
    await test_full_pipeline()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
