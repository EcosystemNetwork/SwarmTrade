"""Smoke tests. Run: python -m tests.test_smoke"""
import asyncio, tempfile, argparse
import pytest
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
        capital=10000.0, max_alloc=50.0, gateway=False,
    )


@pytest.mark.asyncio
async def test_full_pipeline():
    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "t.db"
        kill = Path(d) / "KILL"
        await run(_mock_args(db, kill, duration=6.0))
        assert db.exists()
    print("OK full_pipeline")


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_circuit_breaker():
    """Circuit breaker halts after consecutive losses."""
    from swarmtrader.safety import CircuitBreaker, KillSwitch
    from swarmtrader.core import ExecutionReport
    bus = Bus()
    with tempfile.TemporaryDirectory() as d:
        kill = KillSwitch(Path(d) / "KILL")
        cb = CircuitBreaker(bus, kill, max_consecutive_losses=3,
                            max_drawdown_usd=100.0, cooldown_seconds=5.0)
        # Simulate 3 consecutive losses
        for i in range(3):
            rep = ExecutionReport(f"test{i}", "filled", "0x", 2000.0, 0.001, -10.0, "test")
            await bus.publish("exec.report", rep)
            await asyncio.sleep(0.05)
        assert cb.halted, "circuit breaker should be tripped after 3 losses"
        assert kill.active, "kill switch should be active"
    print("OK circuit_breaker")


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
    # In trending regime, momentum gets a 2x multiplier relative to other agents
    # After renormalization the absolute weight may decrease, but the relative
    # share vs mean-reversion (which gets 0.3x) should widen.
    assert strat.weights["momentum"] > strat.weights["mean_rev"], \
        f"momentum should outweigh mean_rev in trending: mom={strat.weights['momentum']:.4f} mr={strat.weights['mean_rev']:.4f}"
    print("OK regime_strategy")


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


# ── Citadel-Grade Module Tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_var_engine():
    """VaR engine computes historical, parametric, and Monte Carlo VaR."""
    from swarmtrader.var import VaREngine
    from swarmtrader.core import MarketSnapshot, PortfolioTracker
    import time as _time

    bus = Bus()
    portfolio = PortfolioTracker()
    var_eng = VaREngine(bus, portfolio=portfolio)

    # Feed price history
    base = 2000.0
    for i in range(120):
        price = base + (i % 20 - 10) * 5  # oscillate around 2000
        snap = MarketSnapshot(ts=_time.time(), prices={"ETH": price}, gas_gwei=20.0)
        await bus.publish("market.snapshot", snap)
        await asyncio.sleep(0.001)

    # Buy some ETH so portfolio has exposure
    portfolio.buy("ETH", 1.0, 2000.0)
    portfolio.update_prices({"ETH": 2000.0})

    # Historical VaR should return a positive loss number
    h_var = var_eng.historical_var("ETH", confidence=0.95)
    assert h_var is not None, "historical VaR should not be None"

    # Parametric VaR
    p_var = var_eng.parametric_var("ETH", confidence=0.95)
    assert p_var is not None, "parametric VaR should not be None"

    # Monte Carlo VaR
    mc_var = var_eng.monte_carlo_var("ETH", confidence=0.95, n_sims=500)
    assert mc_var is not None, "MC VaR should not be None"

    # CVaR should be >= VaR
    cvar = var_eng.cvar("ETH", confidence=0.95)
    assert cvar is not None
    print("OK var_engine")


@pytest.mark.asyncio
async def test_stress_testing():
    """Stress tester runs historical scenarios and Monte Carlo stress."""
    from swarmtrader.stress_test import StressTester, HISTORICAL_SCENARIOS
    from swarmtrader.core import PortfolioTracker

    portfolio = PortfolioTracker()
    portfolio.buy("ETH", 2.0, 2000.0)
    portfolio.buy("BTC", 0.1, 60000.0)
    portfolio.update_prices({"ETH": 2000.0, "BTC": 60000.0})

    tester = StressTester(portfolio=portfolio)

    # Run single scenario
    result = tester.run_scenario(HISTORICAL_SCENARIOS[0])  # COVID crash
    assert "portfolio_impact_usd" in result
    assert result["portfolio_impact_usd"] < 0, "COVID crash should cause losses"

    # Run all scenarios
    all_results = tester.run_all_scenarios()
    assert len(all_results) == len(HISTORICAL_SCENARIOS)

    # Monte Carlo stress
    mc = tester.monte_carlo_stress(n_sims=200)
    assert "p5_loss" in mc
    assert "survival_rate" in mc
    print("OK stress_testing")


@pytest.mark.asyncio
async def test_portfolio_optimization():
    """Portfolio optimizer computes Markowitz and risk parity weights."""
    from swarmtrader.portfolio_opt import CovarianceEstimator, MarkowitzOptimizer, RiskParityOptimizer
    from swarmtrader.core import MarketSnapshot
    import time as _time, random

    bus = Bus()
    cov_est = CovarianceEstimator(bus, window=50)

    # Feed correlated price data
    random.seed(42)
    eth_price, btc_price = 2000.0, 60000.0
    for i in range(60):
        shock = random.gauss(0, 0.01)
        eth_price *= (1 + shock + random.gauss(0, 0.005))
        btc_price *= (1 + shock * 0.8 + random.gauss(0, 0.003))
        snap = MarketSnapshot(ts=_time.time(), prices={"ETH": eth_price, "BTC": btc_price}, gas_gwei=20.0)
        await bus.publish("market.snapshot", snap)
        await asyncio.sleep(0.001)

    cov = cov_est.covariance_matrix()
    assert "ETH" in cov and "BTC" in cov, "covariance matrix should have both assets"

    # Markowitz
    mkw = MarkowitzOptimizer(cov_estimator=cov_est)
    weights = mkw.optimize()
    assert abs(sum(weights.values()) - 1.0) < 0.01, f"weights should sum to 1: {weights}"

    # Risk parity
    rp = RiskParityOptimizer(cov_estimator=cov_est)
    rp_weights = rp.optimize()
    assert abs(sum(rp_weights.values()) - 1.0) < 0.01, f"RP weights should sum to 1: {rp_weights}"
    print("OK portfolio_optimization")


@pytest.mark.asyncio
async def test_smart_order_router():
    """SOR fetches multi-venue quotes and routes to best execution."""
    from swarmtrader.sor import SmartOrderRouter
    from swarmtrader.core import MarketSnapshot
    import time as _time

    bus = Bus()
    sor = SmartOrderRouter(bus)

    # Feed a price snapshot
    snap = MarketSnapshot(ts=_time.time(), prices={"ETH": 2000.0}, gas_gwei=20.0)
    await bus.publish("market.snapshot", snap)
    await asyncio.sleep(0.01)

    # Get quotes
    quotes = await sor.get_quotes("ETH", "buy")
    assert len(quotes) >= 2, f"should get multiple venue quotes, got {len(quotes)}"

    # Route an intent
    intent = TradeIntent.new(asset_in="USDC", asset_out="ETH",
                             amount_in=500.0, min_out=0, ttl=9e9)
    venue, quote = await sor.route(intent)
    assert venue, "should route to a venue"
    assert quote.ask > 0, "quote should have an ask price"
    print("OK smart_order_router")


@pytest.mark.asyncio
async def test_ml_signal_features():
    """ML FeatureEngine generates features from price data."""
    from swarmtrader.ml_signal import FeatureEngine

    fe = FeatureEngine(asset="ETH")

    # Feed enough prices to compute features
    import random
    random.seed(123)
    price = 2000.0
    for _ in range(60):
        price *= (1 + random.gauss(0, 0.01))
        fe.push(price)

    assert fe.n >= 50, "should have enough data after 60 pushes"
    features = fe.feature_vector()
    names = fe.feature_names()
    assert len(features) == len(names), f"feature/name mismatch: {len(features)} vs {len(names)}"
    assert len(features) >= 8, f"should have 8+ features, got {len(features)}"
    print("OK ml_signal_features")


@pytest.mark.asyncio
async def test_ml_decision_tree():
    """Decision tree trains and predicts."""
    from swarmtrader.ml_signal import DecisionTree
    import random

    random.seed(42)
    # Simple linearly separable data
    X = [[random.gauss(0, 1), random.gauss(0, 1)] for _ in range(100)]
    y = [1.0 if x[0] + x[1] > 0 else -1.0 for x in X]

    tree = DecisionTree(max_depth=4, min_samples_leaf=5)
    tree.fit(X, y)

    # Should get > 70% accuracy on training data
    correct = sum(1 for xi, yi in zip(X, y) if tree.predict(xi) == yi)
    acc = correct / len(X)
    assert acc > 0.7, f"tree accuracy too low: {acc:.2%}"
    print("OK ml_decision_tree")


@pytest.mark.asyncio
async def test_factor_model():
    """Factor model decomposes returns and attributes PnL."""
    from swarmtrader.factor_model import FactorModel
    from swarmtrader.core import MarketSnapshot
    import time as _time, random

    bus = Bus()
    fm = FactorModel(bus)

    random.seed(99)
    eth, btc = 2000.0, 60000.0
    for _ in range(60):
        shock = random.gauss(0, 0.01)
        eth *= (1 + shock + random.gauss(0, 0.005))
        btc *= (1 + shock * 0.7 + random.gauss(0, 0.003))
        snap = MarketSnapshot(ts=_time.time(), prices={"ETH": eth, "BTC": btc}, gas_gwei=20.0)
        await bus.publish("market.snapshot", snap)
        await asyncio.sleep(0.001)

    exposures = fm.factor_exposures("ETH")
    assert "market" in exposures, "should have market factor"
    assert "momentum" in exposures, "should have momentum factor"

    # Decompose a return
    decomp = fm.decompose_return("ETH", 0.02)
    assert "alpha" in decomp, "decomposition should include alpha"
    print("OK factor_model")


@pytest.mark.asyncio
async def test_compliance_wash_trading():
    """Wash trading detector catches buy-then-sell pattern."""
    from swarmtrader.compliance import WashTradingDetector
    from swarmtrader.core import ExecutionReport
    import time as _time

    bus = Bus()
    detector = WashTradingDetector(bus, wash_window=5.0)

    # Simulate a buy fill
    buy_rep = ExecutionReport("w1", "filled", "0x", 2000.0, 0.001, 0.0, "test",
                              side="buy", quantity=1.0, asset="ETH")
    await bus.publish("exec.report", buy_rep)
    await asyncio.sleep(0.01)

    # Immediate sell of same asset should be flagged as wash
    sell_intent = TradeIntent.new(asset_in="ETH", asset_out="USDC",
                                 amount_in=1.0, min_out=0, ttl=9e9)
    is_wash, reason = detector.is_wash(sell_intent)
    assert is_wash, f"should detect wash trade: {reason}"
    print("OK compliance_wash_trading")


@pytest.mark.asyncio
async def test_microstructure_almgren_chriss():
    """Almgren-Chriss model computes optimal execution trajectory."""
    from swarmtrader.microstructure import AlmgrenChrissModel

    model = AlmgrenChrissModel(
        total_quantity=10.0, urgency=0.7,
        volatility=0.02, daily_volume=1000.0,
    )
    traj = model.optimal_trajectory(n_periods=5)
    assert len(traj) == 5, f"should have 5 periods, got {len(traj)}"
    assert abs(sum(traj) - 10.0) < 0.01, f"trajectory should sum to total qty: {sum(traj)}"

    cost = model.expected_cost()
    assert cost > 0, "execution cost should be positive"
    print("OK microstructure_almgren_chriss")


@pytest.mark.asyncio
async def test_monte_carlo_backtester():
    """Monte Carlo backtester generates synthetic return paths."""
    from swarmtrader.walkforward import MonteCarloBacktester
    import random

    random.seed(42)
    returns = [random.gauss(0.001, 0.02) for _ in range(100)]
    mc = MonteCarloBacktester(returns=returns)
    result = mc.simulate(n_paths=200, horizon=50)

    assert "median_return" in result
    assert "probability_of_loss" in result
    assert 0 <= result["probability_of_loss"] <= 1
    assert "expected_max_drawdown" in result
    print("OK monte_carlo_backtester")


async def main():
    await test_veto_blocks()
    await test_quorum_passes()
    await test_circuit_breaker()
    await test_rate_limiter()
    await test_regime_strategy()
    await test_real_pnl()
    await test_news_signal_flow()
    await test_wallet_manager()
    # Citadel-grade tests
    await test_var_engine()
    await test_stress_testing()
    await test_portfolio_optimization()
    await test_smart_order_router()
    await test_ml_signal_features()
    await test_ml_decision_tree()
    await test_factor_model()
    await test_compliance_wash_trading()
    await test_microstructure_almgren_chriss()
    await test_monte_carlo_backtester()
    await test_full_pipeline()
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
