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
        dashboard=False, no_advanced=True, web=False, web_port=8080,
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


async def test_circuit_breaker():
    """Circuit breaker halts after consecutive losses."""
    from swarmtrader.safety import CircuitBreaker
    from swarmtrader.core import ExecutionReport
    bus = Bus()
    with tempfile.TemporaryDirectory() as d:
        kill = Path(d) / "KILL"
        cb = CircuitBreaker(bus, kill, max_consecutive_losses=3,
                            max_drawdown_usd=100.0, cooldown_seconds=5.0)
        # Simulate 3 consecutive losses
        for i in range(3):
            rep = ExecutionReport(f"test{i}", "filled", "0x", 2000.0, 0.001, -10.0, "test")
            await bus.publish("exec.report", rep)
            await asyncio.sleep(0.05)
        assert cb.halted, "circuit breaker should be tripped after 3 losses"
        assert kill.exists(), "kill switch file should be created"
    print("OK circuit_breaker")


async def test_rate_limiter():
    """Rate limiter blocks after max trades."""
    from swarmtrader.risk import RateLimiter
    from swarmtrader.core import ExecutionReport
    bus = Bus()
    rl = RateLimiter(bus, max_trades=3, window_s=60.0)
    # Simulate 3 fills
    for i in range(3):
        rep = ExecutionReport(f"rl{i}", "filled", "0x", 2000.0, 0.001, 1.0, "test")
        await bus.publish("exec.report", rep)
        await asyncio.sleep(0.05)
    intent = TradeIntent.new(asset_in="USDC", asset_out="ETH",
                             amount_in=100.0, min_out=0, ttl=9e9)
    ok, reason = rl.check(intent)
    assert not ok, f"rate limiter should block: {reason}"
    print("OK rate_limiter")


async def test_regime_strategy():
    """Strategist adjusts weights based on regime signals."""
    from swarmtrader.strategy import Strategist
    from swarmtrader.core import Signal
    bus = Bus()
    strat = Strategist(bus, base_size=500.0)
    # Send regime signal
    sig = Signal("regime", "ETH", "flat", 0.0, 0.8, "regime=trending adx=0.5 hurst=0.7")
    await bus.publish("signal.regime", sig)
    await asyncio.sleep(0.05)
    assert strat.regime == "trending", f"expected trending, got {strat.regime}"
    # Momentum should be boosted in trending regime
    assert strat.weights["momentum"] > strat.DEFAULT_WEIGHTS["momentum"], \
        f"momentum weight should increase in trending: {strat.weights['momentum']}"
    print("OK regime_strategy")


async def main():
    await test_veto_blocks()
    await test_quorum_passes()
    await test_circuit_breaker()
    await test_rate_limiter()
    await test_regime_strategy()
    await test_full_pipeline()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
