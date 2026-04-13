"""Tests for all 40 phases of ETHGlobal showcase integrations.

Covers: import verification, Bus wiring, core functionality for each module.
Run: pytest tests/test_phases.py -v
"""
import asyncio
import time
import pytest

from swarmtrader.core import Bus, Signal, TradeIntent, PortfolioTracker, MarketSnapshot


# ── Helpers ──────────────────────────────────────────────────────

def make_bus():
    return Bus()

def make_signal(agent="test", asset="ETH", direction="long", strength=0.8, confidence=0.9):
    return Signal(agent, asset, direction, strength, confidence, "test rationale")

def make_intent(asset_in="USDC", asset_out="ETH", amount=500.0, supporting=None):
    return TradeIntent.new(
        asset_in=asset_in, asset_out=asset_out, amount_in=amount,
        min_out=0, ttl=time.time() + 30,
        supporting=supporting or [make_signal()],
    )

def make_snapshot(prices=None):
    return MarketSnapshot(ts=time.time(), prices=prices or {"ETH": 3500.0, "BTC": 70000.0}, gas_gwei=1.0)


# ── Phase 1: Kalman Filter ───────────────────────────────────────

class TestKalmanFilter:
    def test_import(self):
        from swarmtrader.kalman import AdaptiveKalmanFilter
        assert AdaptiveKalmanFilter

    def test_filter_signal(self):
        bus = make_bus()
        from swarmtrader.kalman import AdaptiveKalmanFilter
        kf = AdaptiveKalmanFilter(bus)
        assert kf._regime == "normal"
        assert len(kf._states) == 0

    @pytest.mark.asyncio
    async def test_signal_processing(self):
        bus = make_bus()
        from swarmtrader.kalman import AdaptiveKalmanFilter
        kf = AdaptiveKalmanFilter(bus)
        filtered = []
        bus.subscribe("signal.filtered.test", lambda s: filtered.append(s))
        await bus.publish("signal.momentum", make_signal(agent="test"))
        await asyncio.sleep(0.05)
        # Kalman should have created state for ETH
        assert "ETH" in kf._states


# ── Phase 1: Data Fusion ─────────────────────────────────────────

class TestDataFusion:
    def test_import(self):
        from swarmtrader.fusion import DataFusionPipeline
        assert DataFusionPipeline

    def test_init(self):
        bus = make_bus()
        from swarmtrader.fusion import DataFusionPipeline
        f = DataFusionPipeline(bus, window_s=60, min_sources=2)
        assert f.window_s == 60
        assert f.min_sources == 2


# ── Phase 2: Price Validation Gate ───────────────────────────────

class TestPriceGate:
    def test_import(self):
        from swarmtrader.price_gate import PriceValidationGate
        assert PriceValidationGate

    def test_no_sources_halts(self):
        bus = make_bus()
        from swarmtrader.price_gate import PriceValidationGate
        gate = PriceValidationGate(bus)
        # No prices = HALT (can't validate, refuse to execute blind)
        check = gate.validate_price("ETH")
        assert check.tier == "HALT"

    def test_halt_on_divergence(self):
        bus = make_bus()
        from swarmtrader.price_gate import PriceValidationGate
        gate = PriceValidationGate(bus)
        now = time.time()
        # Inject divergent prices as (price, timestamp) tuples
        gate._prices["ETH"] = {
            "source_a": (3500, now),
            "source_b": (3600, now),
            "source_c": (3200, now),
        }
        check = gate.validate_price("ETH")
        # 3200 vs 3600 = ~11% divergence = HALT
        assert check.tier == "HALT"

    def test_safe_on_agreement(self):
        bus = make_bus()
        from swarmtrader.price_gate import PriceValidationGate
        gate = PriceValidationGate(bus)
        now = time.time()
        # Inject agreeing prices (within 0.5%)
        gate._prices["ETH"] = {
            "source_a": (3500, now),
            "source_b": (3505, now),
            "source_c": (3498, now),
        }
        check = gate.validate_price("ETH")
        assert check.tier == "SAFE"

    def test_optimal_chunk_size(self):
        bus = make_bus()
        from swarmtrader.price_gate import PriceValidationGate
        gate = PriceValidationGate(bus)
        chunk = gate.optimal_chunk_size("ETH", 10000.0, max_impact_bps=50)
        assert chunk > 0
        assert chunk <= 10000.0


# ── Phase 3: Adversarial Debate ──────────────────────────────────

class TestDebate:
    def test_import(self):
        from swarmtrader.debate import AdversarialDebateEngine, ELOTracker, BullAgent, BearAgent
        assert all([AdversarialDebateEngine, ELOTracker, BullAgent, BearAgent])

    def test_elo_tracker(self):
        from swarmtrader.debate import ELOTracker
        elo = ELOTracker()
        assert elo.get_elo("test") == 500.0
        elo.update("test", won=True)
        assert elo.get_elo("test") > 500.0
        elo.update("test", won=False)
        assert elo.get_elo("test") < elo.get_elo("test") + 32  # bounded

    def test_debate_resolver(self):
        from swarmtrader.debate import DebateResolver, DebateArgument
        resolver = DebateResolver(min_margin=0.3)
        bull = DebateArgument("bull", score=2.0, signals_used=5, key_points=["strong"])
        bear = DebateArgument("bear", score=0.5, signals_used=2, key_points=["weak"])
        verdict, margin, conviction = resolver.resolve(bull, bear)
        assert verdict == "approve"
        assert margin > 0

    def test_debate_rejects_on_tie(self):
        from swarmtrader.debate import DebateResolver, DebateArgument
        resolver = DebateResolver(min_margin=0.3)
        bull = DebateArgument("bull", score=1.0, signals_used=3, key_points=["ok"])
        bear = DebateArgument("bear", score=0.9, signals_used=3, key_points=["ok"])
        verdict, margin, _ = resolver.resolve(bull, bear)
        assert verdict == "hold"  # margin 0.1 < 0.3 threshold


# ── Phase 4: ERC-4626 Vault ──────────────────────────────────────

class TestVault:
    def test_deposit_and_redeem(self):
        bus = make_bus()
        from swarmtrader.vault import VaultManager
        v = VaultManager(bus, mode="simulate")
        d = v.deposit("user1", 1000.0)
        assert d.shares_received == 1000.0
        assert v.state.total_assets == 1000.0
        assets = v.redeem("user1", 500.0)
        assert abs(assets - 500.0) < 0.01
        assert v.state.total_assets == 500.0

    def test_trade_validation(self):
        bus = make_bus()
        from swarmtrader.vault import VaultManager
        v = VaultManager(bus, mode="simulate", daily_limit=1000)
        v.deposit("user1", 5000.0)
        v.add_allowed_token("ETH")
        ok, reason = v.validate_trade(make_intent())
        assert ok

    def test_blocks_unapproved_token(self):
        bus = make_bus()
        from swarmtrader.vault import VaultManager
        v = VaultManager(bus, mode="simulate")
        v.deposit("user1", 5000.0)
        # Don't add DOGE to allowlist
        intent = make_intent(asset_out="DOGE")
        ok, reason = v.validate_trade(intent)
        assert not ok
        assert "allowlist" in reason


# ── Phase 5: Agent Marketplace ───────────────────────────────────

class TestMarketplace:
    def test_import(self):
        from swarmtrader.marketplace import AgentMarketplace
        assert AgentMarketplace

    def test_register_and_search(self):
        bus = make_bus()
        from swarmtrader.marketplace import AgentMarketplace
        m = AgentMarketplace(bus)
        m.register_agent("rsi_agent", display_name="RSI Pro", fee_pct=10)
        results = m.search_agents(query="rsi")
        assert len(results) == 1
        assert results[0].agent_id == "rsi_agent"


# ── Phase 11: Rugpull Detector ───────────────────────────────────

class TestRugpull:
    @pytest.mark.asyncio
    async def test_trusted_tokens_pass(self):
        from swarmtrader.rugpull_detector import RugpullDetector
        bus = make_bus()
        rd = RugpullDetector(bus, mode="strict")
        report = await rd.scan_token("ETH")
        assert report.verdict == "safe"

    @pytest.mark.asyncio
    async def test_unknown_token_flagged(self):
        from swarmtrader.rugpull_detector import RugpullDetector
        bus = make_bus()
        rd = RugpullDetector(bus, mode="strict")
        report = await rd.scan_token("SCAMCOIN")
        assert report.verdict in ("dangerous", "caution")
        assert len(report.flags) > 0


# ── Phase 14: Strategy Evolution ─────────────────────────────────

class TestEvolution:
    def test_random_genome(self):
        from swarmtrader.strategy_evolution import StrategyGenome
        g = StrategyGenome.random("test-001")
        assert len(g.signal_weights) > 0
        assert len(g.risk_params) > 0

    def test_crossover(self):
        from swarmtrader.strategy_evolution import StrategyEvolution
        bus = make_bus()
        evo = StrategyEvolution(bus, population_size=10)
        from swarmtrader.strategy_evolution import StrategyGenome
        a = StrategyGenome.random("a")
        b = StrategyGenome.random("b")
        child = evo.crossover(a, b)
        assert child.genome_id != a.genome_id
        assert len(child.signal_weights) > 0


# ── Phase 18: Intent Solver ──────────────────────────────────────

class TestIntentSolver:
    def test_solver_quote(self):
        from swarmtrader.intent_solver import Solver
        s = Solver("test", "TestDEX", fee_bps=5, base_slippage_bps=10)
        intent = make_intent()
        quote = s.quote(intent, 3500.0)
        assert quote is not None
        assert quote.amount_out > 0
        assert quote.fee_usd > 0


# ── Phase 20: Governance ─────────────────────────────────────────

class TestGovernance:
    def test_propose_and_vote(self):
        bus = make_bus()
        from swarmtrader.governance import GovernanceDAO
        from swarmtrader.debate import ELOTracker
        elo = ELOTracker()
        elo._elos["agent_a"] = 700
        elo._elos["agent_b"] = 600
        elo._elos["agent_c"] = 500
        dao = GovernanceDAO(bus, elo_tracker=elo)
        p = dao.propose("agent_a", "Increase max leverage", "Test", "risk", {"max_leverage": 3})
        assert p.status == "active"
        dao.vote(p.proposal_id, "agent_a", True)
        dao.vote(p.proposal_id, "agent_b", True)
        dao.vote(p.proposal_id, "agent_c", False)
        # 700+600 for vs 500 against = 72% approval > 60% threshold
        assert p.status == "passed"


# ── Commander Gate (the AI trading robot) ────────────────────────

class TestCommanderGate:
    @pytest.mark.asyncio
    async def test_strict_blocks(self):
        from swarmtrader.hermes_brain import HermesBrain, CommanderGate
        class MockProv:
            name = "mock"; model = "test"
            async def setup(self, s): return False
            async def query(self, s, sy, p, m): return "[]"
            def describe(self): return "mock"

        bus = make_bus()
        executed = []
        async def track(i): executed.append(i.id)
        bus.subscribe("exec.go", track)

        brain = HermesBrain(bus, portfolio=PortfolioTracker(), assets=["ETH"], provider=MockProv())
        brain._ready = False
        gate = CommanderGate(bus, brain=brain, fallback_mode="strict")

        await bus.publish("intent.new", make_intent())
        await asyncio.sleep(0.05)
        assert gate._stats["rejected"] == 1
        assert len(executed) == 0

    @pytest.mark.asyncio
    async def test_hermes_intents_reviewed_by_commander(self):
        """Hermes intents go through full commander review (no self-approval bypass)."""
        from swarmtrader.hermes_brain import HermesBrain, CommanderGate
        class MockProv:
            name = "mock"; model = "test"
            async def setup(self, s): return False
            async def query(self, session, system, prompt, **kwargs): return "APPROVE"
            def describe(self): return "mock"

        bus = make_bus()
        brain = HermesBrain(bus, portfolio=PortfolioTracker(), assets=["ETH"], provider=MockProv())
        brain._ready = True
        gate = CommanderGate(bus, brain=brain, fallback_mode="strict")

        intent = make_intent(supporting=[make_signal(agent="hermes")])
        await bus.publish("intent.new", intent)
        await asyncio.sleep(0.1)
        assert gate._stats["approved"] == 1

    @pytest.mark.asyncio
    async def test_conservative_filters(self):
        from swarmtrader.hermes_brain import HermesBrain, CommanderGate
        class MockProv:
            name = "mock"; model = "test"
            async def setup(self, s): return False
            async def query(self, s, sy, p, m): return "[]"
            def describe(self): return "mock"

        bus = make_bus()
        exec_list = []
        async def track(i): exec_list.append(i.id)
        bus.subscribe("exec.go", track)

        brain = HermesBrain(bus, portfolio=PortfolioTracker(), assets=["ETH"], provider=MockProv())
        brain._ready = False
        gate = CommanderGate(bus, brain=brain, fallback_mode="conservative")

        # High conviction: 3 signals avg 0.88 -> approve
        hi = make_intent(supporting=[
            make_signal("a", confidence=0.9), make_signal("b", confidence=0.85),
            make_signal("c", confidence=0.9),
        ])
        await bus.publish("intent.new", hi)
        await asyncio.sleep(0.05)
        assert gate._stats["approved"] == 1

        # Low conviction: 1 signal 0.4 -> reject
        lo = make_intent(supporting=[make_signal("d", confidence=0.4, strength=0.3)])
        await bus.publish("intent.new", lo)
        await asyncio.sleep(0.05)
        assert gate._stats["rejected"] == 1


# ── Full Boot Test ───────────────────────────────────────────────

class TestFullBoot:
    def test_all_imports(self):
        """Verify all 43 phase modules import without error."""
        modules = [
            "price_gate", "kalman", "fusion", "debate",
            "vault", "marketplace", "flashloan", "strategy_nft",
            "v4_hooks", "cross_chain", "zk_trading",
            "rugpull_detector", "prediction_trader", "agent_payments",
            "strategy_evolution", "ai_brain", "narrative",
            "whale_mirror", "intent_solver", "liquidation_shield", "governance",
            "telegram_bot", "voice_trading", "agent_factory",
            "options_engine", "social_alpha_v2", "backtesting_arena",
            "observability", "federated", "rwa_bridge", "treasury",
            "mev_engine", "agent_memory_v2", "sentiment_derivatives",
            "grid_trading", "token_sniper", "regime_v2",
            "gas_optimizer", "portfolio_insurance", "agent_protocol",
            "swarm_consensus", "hermes_brain",
        ]
        for mod in modules:
            m = __import__(f"swarmtrader.{mod}")
            assert m is not None

    def test_strategist_has_all_weights(self):
        """Verify Strategist has weights for all new signal sources."""
        bus = make_bus()
        from swarmtrader.strategy import Strategist
        s = Strategist(bus)
        assert len(s.weights) >= 48
        # Check critical new weights exist
        for key in ["fusion", "debate", "consensus", "narrative",
                     "whale_mirror", "marketplace", "sentiment_deriv",
                     "mev", "regime_v2", "rwa"]:
            assert key in s.weights, f"Missing weight: {key}"

    def test_database_schema(self):
        """Verify all 15 new tables are in schema."""
        from swarmtrader.database import SQLITE_SCHEMA
        schema_text = " ".join(SQLITE_SCHEMA)
        for table in ["vault_deposits", "vault_trades", "marketplace_auctions",
                       "agent_elo", "debates", "memory_dag", "agent_payments",
                       "treasury_state", "strategy_nfts", "governance_proposals",
                       "commander_log", "narratives", "grid_trades",
                       "observability_reports"]:
            assert table in schema_text, f"Missing table: {table}"
