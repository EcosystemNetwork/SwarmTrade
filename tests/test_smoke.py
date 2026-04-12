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
        max_positions=5, hard_stop=0.05, trail_stop=0.03, max_hold=3600.0,
        capital=10000.0, max_alloc=50.0,
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


async def test_real_pnl():
    """PortfolioTracker computes real PnL from price deltas, not signal proxies."""
    from swarmtrader.core import PortfolioTracker
    pt = PortfolioTracker()

    # Buy 1 ETH at $2000
    fee1, pnl1 = pt.buy("ETH", 1.0, 2000.0)
    assert pnl1 == 0.0, "buys should not realize PnL"
    assert fee1 > 0, "buy should charge a fee"
    assert abs(pt.get("ETH").quantity - 1.0) < 1e-9
    assert abs(pt.get("ETH").avg_entry - 2000.0) < 1e-9

    # Buy 1 more ETH at $2200 (avg entry should be $2100)
    pt.buy("ETH", 1.0, 2200.0)
    assert abs(pt.get("ETH").quantity - 2.0) < 1e-9
    assert abs(pt.get("ETH").avg_entry - 2100.0) < 1e-9

    # Sell 1 ETH at $2300 → PnL = (2300 - 2100) * 1 - fee
    fee3, pnl3 = pt.sell("ETH", 1.0, 2300.0)
    assert pnl3 > 0, f"should be profitable: got {pnl3}"
    expected_gross = (2300 - 2100) * 1.0  # $200 gross
    assert pnl3 < expected_gross, "PnL should be less than gross after fees"
    assert abs(pt.get("ETH").quantity - 1.0) < 1e-9

    # Sell remaining at $1900 → loss
    fee4, pnl4 = pt.sell("ETH", 1.0, 1900.0)
    assert pnl4 < 0, f"should be a loss: got {pnl4}"
    assert abs(pt.get("ETH").quantity) < 1e-9, "should be flat"

    # Total realized PnL should reflect both trades
    total = pt.total_realized_pnl()
    assert abs(total - (pnl3 + pnl4)) < 1e-9

    print("OK real_pnl")


async def test_news_signal_flow():
    """NewsAgent keyword analysis produces correct signals that reach the Strategist."""
    from swarmtrader.news import NewsAgent
    from swarmtrader.strategy import Strategist
    from swarmtrader.core import Signal

    bus = Bus()
    received = []
    bus.subscribe("signal.news", lambda sig: received.append(sig))

    agent = NewsAgent(bus, assets=["ETH"], interval=999)

    # Simulate scoring articles directly (no API needed)
    mock_articles = [
        {"title": "Bitcoin ETF approved by SEC", "votes": {"positive": 10, "negative": 1,
         "important": 5, "liked": 3, "disliked": 0}, "url": "https://a.com/1", "kind": "news"},
        {"title": "Ethereum rally continues", "votes": {"positive": 4, "negative": 1,
         "important": 2, "liked": 2, "disliked": 0}, "url": "https://a.com/2", "kind": "bullish"},
    ]
    scored = agent._score_articles(mock_articles)
    assert len(scored) == 2, f"expected 2 scored articles, got {len(scored)}"

    # Both articles are bullish — aggregated signal should be long
    sig = agent._aggregate_signal("ETH", scored)
    assert sig is not None, "should produce a signal"
    assert sig.direction == "long", f"expected long, got {sig.direction}"
    assert sig.strength > 0.1, f"strength too low: {sig.strength}"
    assert "URGENT" in sig.rationale, f"ETF approval should trigger urgency: {sig.rationale}"

    # Verify keyword detection for bearish headlines
    bearish_articles = [
        {"title": "Major exchange hacked, $50M drained", "votes": {},
         "url": "https://b.com/1", "kind": "news"},
        {"title": "SEC files lawsuit against exchange for fraud", "votes": {},
         "url": "https://b.com/2", "kind": "news"},
    ]
    scored_bear = agent._score_articles(bearish_articles)
    sig_bear = agent._aggregate_signal("ETH", scored_bear)
    assert sig_bear is not None, "should produce bearish signal"
    assert sig_bear.direction == "short", f"expected short, got {sig_bear.direction}"

    # Verify Strategist receives signal.news
    strat = Strategist(bus, base_size=500.0)
    test_sig = Signal("news", "ETH", "long", 0.6, 0.8, "test news signal")
    await bus.publish("signal.news", test_sig)
    await asyncio.sleep(0.05)
    assert "news" in strat.latest, "Strategist should receive news signals"
    assert strat.latest["news"].strength == 0.6

    print("OK news_signal_flow")


async def test_wallet_manager():
    """WalletManager tracks cash, reserves, allocations, and persists state."""
    from swarmtrader.core import PortfolioTracker
    from swarmtrader.wallet import WalletManager, funds_check, allocation_check

    bus_w = Bus()
    portfolio = PortfolioTracker()
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "wallet.db"
        wallet = WalletManager(
            bus_w, portfolio,
            starting_capital=10_000.0,
            db_path=db,
            allocations={"ETH": {"target_pct": 30.0, "max_pct": 50.0}},
        )

        # Initial state
        assert wallet.cash_balance == 10_000.0
        assert wallet.available_cash() == 10_000.0
        assert wallet.total_equity() == 10_000.0

        # Deposit / withdraw
        wallet.deposit(5_000.0, "test deposit")
        assert wallet.cash_balance == 15_000.0
        result = wallet.withdraw(2_000.0)
        assert result == 13_000.0
        assert wallet.withdraw(999_999.0) is None  # insufficient

        # Funds check
        check_fn = funds_check(wallet)
        ok, _ = check_fn(TradeIntent.new(asset_in="USDC", asset_out="ETH",
                                         amount_in=5_000.0, min_out=0, ttl=9e9))
        assert ok, "should afford 5000"
        ok, _ = check_fn(TradeIntent.new(asset_in="USDC", asset_out="ETH",
                                         amount_in=50_000.0, min_out=0, ttl=9e9))
        assert not ok, "should not afford 50000"

        # Reserve funds
        assert wallet.reserve("r1", 3_000.0, "ETH", "buy")
        assert wallet.available_cash() == 10_000.0  # 13000 - 3000
        assert wallet.release_reserve("r1") == 3_000.0

        # Summary
        summary = wallet.summary()
        assert "cash_balance" in summary
        assert "ETH" in summary["allocations"]
        assert summary["allocations"]["ETH"]["max_pct"] == 50.0

        # Persistence
        wallet2 = WalletManager(bus_w, portfolio, starting_capital=999.0, db_path=db)
        assert abs(wallet2.cash_balance - 13_000.0) < 0.01

    print("OK wallet_manager")


async def main():
    await test_veto_blocks()
    await test_quorum_passes()
    await test_circuit_breaker()
    await test_rate_limiter()
    await test_regime_strategy()
    await test_real_pnl()
    await test_news_signal_flow()
    await test_wallet_manager()
    await test_full_pipeline()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
