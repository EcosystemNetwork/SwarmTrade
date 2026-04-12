"""Agent Marketplace — competitive signal scoring and agent commerce.

Inspired by Grand Bazaar (monorepo agent marketplace), Nimble (competitive
solver auctions), AgentHive (orchestrator hires specialists), and GhostFi
(strategies as tradeable iNFTs).

Key patterns:
  - Competitive signal scoring: multiple agents bid signals, highest wins
  - Performance-based fees: only winning signals earn revenue
  - Agent discovery: browse by APY, Sharpe, ELO, asset class
  - Task decomposition: orchestrator agents hire specialists
  - Reputation-as-capital: high ELO = more trust = more allocation

Bus integration:
  Subscribes to: signal.* (collects competing signals)
                 exec.report (tracks outcomes for fee distribution)
  Publishes to:  marketplace.winner (winning signal selected)
                 marketplace.fee (fee attribution for winning agent)

No external dependencies.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from collections import defaultdict

from .core import Bus, Signal, ExecutionReport

log = logging.getLogger("swarm.marketplace")


@dataclass
class AgentListing:
    """An agent's marketplace profile."""
    agent_id: str
    display_name: str
    description: str = ""
    # Performance metrics
    total_signals: int = 0
    winning_signals: int = 0
    total_pnl: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    # Fee structure
    fee_pct: float = 10.0       # % of profit earned by agent creator
    min_confidence: float = 0.3  # minimum confidence to participate in auctions
    # Classification
    asset_classes: list[str] = field(default_factory=lambda: ["crypto"])
    strategy_tags: list[str] = field(default_factory=list)
    # Status
    elo: float = 500.0
    tier: str = "mid"           # "high", "mid", "low"
    active: bool = True
    registered_at: float = field(default_factory=time.time)
    last_signal: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.winning_signals / max(self.total_signals, 1)

    @property
    def avg_pnl_per_signal(self) -> float:
        return self.total_pnl / max(self.winning_signals, 1)


@dataclass
class SignalBid:
    """A competing signal bid in an auction."""
    agent_id: str
    signal: Signal
    bid_score: float       # composite score for ranking
    elo_weight: float
    ts: float = field(default_factory=time.time)


@dataclass
class AuctionResult:
    """Result of a competitive signal auction."""
    auction_id: str
    asset: str
    winner: SignalBid
    runner_up: SignalBid | None
    total_bids: int
    margin: float              # score gap between 1st and 2nd
    ts: float = field(default_factory=time.time)


@dataclass
class FeeAttribution:
    """Fee earned by an agent for a winning signal."""
    agent_id: str
    intent_id: str
    pnl: float
    fee_earned: float
    fee_pct: float
    ts: float = field(default_factory=time.time)


class AgentMarketplace:
    """Competitive marketplace where agents bid signals and earn fees.

    Auction flow:
      1. Multiple agents publish signals for the same asset within a window
      2. Marketplace scores each signal: strength * confidence * ELO_weight
      3. Highest-scoring signal wins execution
      4. If the resulting trade is profitable, winning agent earns fee_pct of profit
      5. Agent's marketplace metrics updated (win rate, PnL, Sharpe)

    The key insight from Nimble: competition naturally filters bad agents.
    Only signals that lead to profit earn fees, creating alignment.
    """

    def __init__(self, bus: Bus, auction_window_s: float = 10.0,
                 min_bids: int = 1, elo_tracker=None):
        self.bus = bus
        self.auction_window_s = auction_window_s
        self.min_bids = min_bids
        self.elo_tracker = elo_tracker
        self._listings: dict[str, AgentListing] = {}
        self._pending_bids: dict[str, list[SignalBid]] = {}  # asset -> bids
        self._bid_timestamps: dict[str, float] = {}  # asset -> first bid ts
        self._auctions: list[AuctionResult] = []
        self._fees: list[FeeAttribution] = []
        self._pending_outcomes: dict[str, AuctionResult] = {}  # intent_id -> result
        self._auction_counter = 0
        self._stats = {"auctions": 0, "total_fees": 0.0}

        # Collect signals from all sources
        for topic in [
            "signal.momentum", "signal.mean_reversion", "signal.rsi",
            "signal.macd", "signal.bollinger", "signal.whale",
            "signal.news", "signal.ml", "signal.confluence",
            "signal.smart_money", "signal.fusion", "signal.debate",
            "signal.sentiment", "signal.onchain", "signal.funding_rate",
            "signal.orderbook", "signal.fear_greed",
            "signal.arbitrage", "signal.correlation",
        ]:
            bus.subscribe(topic, self._on_signal)

        bus.subscribe("exec.report", self._on_execution)

    # ── Agent Registration ───────────────────────────────────────

    def register_agent(self, agent_id: str, display_name: str = "",
                       fee_pct: float = 10.0, **kwargs) -> AgentListing:
        """Register an agent in the marketplace."""
        listing = AgentListing(
            agent_id=agent_id,
            display_name=display_name or agent_id,
            fee_pct=fee_pct,
            **kwargs,
        )
        self._listings[agent_id] = listing
        log.info("Marketplace: registered agent %s (fee=%.1f%%)", agent_id, fee_pct)
        return listing

    def get_listing(self, agent_id: str) -> AgentListing:
        """Get or create a listing for an agent."""
        if agent_id not in self._listings:
            self._listings[agent_id] = AgentListing(
                agent_id=agent_id, display_name=agent_id,
            )
        return self._listings[agent_id]

    # ── Signal Auction ───────────────────────────────────────────

    async def _on_signal(self, sig: Signal):
        """Collect incoming signals as bids for competitive auction."""
        if not isinstance(sig, Signal):
            return
        if sig.confidence < 0.1 or abs(sig.strength) < 0.05:
            return

        listing = self.get_listing(sig.agent_id)
        listing.total_signals += 1
        listing.last_signal = time.time()

        # ELO weight
        elo = 500.0
        if self.elo_tracker:
            elo = self.elo_tracker.get_elo(sig.agent_id)
        elo_weight = 0.5 + (elo / 1000.0)

        # Composite bid score
        bid_score = sig.strength * sig.confidence * elo_weight

        bid = SignalBid(
            agent_id=sig.agent_id, signal=sig,
            bid_score=round(bid_score, 4), elo_weight=round(elo_weight, 3),
        )

        asset = sig.asset
        if asset not in self._pending_bids:
            self._pending_bids[asset] = []
            self._bid_timestamps[asset] = time.time()

        self._pending_bids[asset].append(bid)

        # Check if auction window has closed
        elapsed = time.time() - self._bid_timestamps.get(asset, time.time())
        if elapsed >= self.auction_window_s:
            await self._resolve_auction(asset)

    async def _resolve_auction(self, asset: str):
        """Resolve a signal auction for an asset — highest bid wins."""
        bids = self._pending_bids.pop(asset, [])
        self._bid_timestamps.pop(asset, None)

        if len(bids) < self.min_bids:
            return

        # Sort by bid score descending
        bids.sort(key=lambda b: b.bid_score, reverse=True)
        winner = bids[0]
        runner_up = bids[1] if len(bids) > 1 else None

        margin = winner.bid_score - (runner_up.bid_score if runner_up else 0.0)

        self._auction_counter += 1
        auction_id = f"auction-{self._auction_counter:05d}"

        result = AuctionResult(
            auction_id=auction_id,
            asset=asset,
            winner=winner,
            runner_up=runner_up,
            total_bids=len(bids),
            margin=round(margin, 4),
        )
        self._auctions.append(result)
        if len(self._auctions) > 500:
            self._auctions = self._auctions[-250:]

        self._stats["auctions"] += 1

        log.info(
            "AUCTION %s: %s | winner=%s (score=%.3f) beats %d others by %.3f",
            auction_id, asset, winner.agent_id, winner.bid_score,
            len(bids) - 1, margin,
        )

        # Publish winning signal
        await self.bus.publish("marketplace.winner", {
            "auction": result,
            "signal": winner.signal,
        })

        # Also publish as a regular signal so Strategist can consume it
        boosted = Signal(
            agent_id=f"market_{winner.agent_id}",
            asset=winner.signal.asset,
            direction=winner.signal.direction,
            strength=min(1.0, winner.signal.strength * 1.1),  # slight boost for winning
            confidence=winner.signal.confidence,
            rationale=f"Auction winner ({len(bids)} bids, margin={margin:.3f}): {winner.signal.rationale[:80]}",
        )
        await self.bus.publish("signal.marketplace", boosted)

    # ── Fee Distribution ─────────────────────────────────────────

    async def _on_execution(self, report: ExecutionReport):
        """Distribute fees to winning agents based on trade outcomes."""
        if report.status != "filled":
            return

        pnl = report.pnl_estimate or 0.0
        if pnl <= 0:
            return  # No fees on losing trades

        # Find the auction that led to this trade
        # Match by asset (simplification — production would use intent_id tracking)
        asset = report.asset
        recent_auctions = [
            a for a in self._auctions[-20:]
            if a.asset == asset and time.time() - a.ts < 300
        ]
        if not recent_auctions:
            return

        auction = recent_auctions[-1]
        listing = self.get_listing(auction.winner.agent_id)

        fee = pnl * (listing.fee_pct / 100.0)
        listing.winning_signals += 1
        listing.total_pnl += pnl

        attr = FeeAttribution(
            agent_id=listing.agent_id,
            intent_id=report.intent_id,
            pnl=round(pnl, 4),
            fee_earned=round(fee, 4),
            fee_pct=listing.fee_pct,
        )
        self._fees.append(attr)
        if len(self._fees) > 1000:
            self._fees = self._fees[-500:]

        self._stats["total_fees"] += fee

        log.info(
            "MARKETPLACE FEE: %s earned $%.4f (%.1f%% of $%.4f PnL) from auction %s",
            listing.agent_id, fee, listing.fee_pct, pnl, auction.auction_id,
        )

        await self.bus.publish("marketplace.fee", attr)

    # ── Discovery ────────────────────────────────────────────────

    def leaderboard(self, sort_by: str = "pnl", top_n: int = 20) -> list[dict]:
        """Get marketplace leaderboard."""
        listings = [l for l in self._listings.values() if l.total_signals > 0]

        if sort_by == "pnl":
            listings.sort(key=lambda l: l.total_pnl, reverse=True)
        elif sort_by == "win_rate":
            listings.sort(key=lambda l: l.win_rate, reverse=True)
        elif sort_by == "elo":
            listings.sort(key=lambda l: l.elo, reverse=True)
        elif sort_by == "sharpe":
            listings.sort(key=lambda l: l.sharpe_ratio, reverse=True)

        return [
            {
                "agent_id": l.agent_id,
                "display_name": l.display_name,
                "total_pnl": round(l.total_pnl, 4),
                "win_rate": round(l.win_rate, 3),
                "signals": l.total_signals,
                "wins": l.winning_signals,
                "elo": l.elo,
                "fee_pct": l.fee_pct,
                "tags": l.strategy_tags,
            }
            for l in listings[:top_n]
        ]

    def search_agents(self, query: str = "", asset_class: str = "",
                      min_elo: float = 0, active_only: bool = True) -> list[AgentListing]:
        """Search for agents matching criteria."""
        results = []
        for listing in self._listings.values():
            if active_only and not listing.active:
                continue
            if min_elo and listing.elo < min_elo:
                continue
            if asset_class and asset_class not in listing.asset_classes:
                continue
            if query:
                q = query.lower()
                if (q not in listing.agent_id.lower() and
                    q not in listing.display_name.lower() and
                    q not in listing.description.lower() and
                    not any(q in t.lower() for t in listing.strategy_tags)):
                    continue
            results.append(listing)
        return results

    def summary(self) -> dict:
        return {
            "total_agents": len(self._listings),
            "active_agents": len([l for l in self._listings.values() if l.active]),
            **self._stats,
            "leaderboard": self.leaderboard(top_n=5),
            "recent_auctions": [
                {
                    "id": a.auction_id,
                    "asset": a.asset,
                    "winner": a.winner.agent_id,
                    "bids": a.total_bids,
                    "margin": a.margin,
                }
                for a in self._auctions[-5:]
            ],
        }
