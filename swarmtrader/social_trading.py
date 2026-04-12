"""Social trading layer — profiles, copy trading, revenue sharing, referrals, feed.

Turns SwarmTrader from a solo trading engine into a social platform where:
- Agents publish strategies and build public track records
- Other agents copy trades with configurable risk controls
- Signal providers earn performance fees from copiers (2/20 model)
- Referral codes create agent-to-agent affiliate revenue
- Activity feed shows trades, follows, milestones in real-time
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from .core import Bus, Signal, TradeIntent, ExecutionReport

log = logging.getLogger("swarm.social")

# ── Data Types ────────────────────────────────────────────────────

ProfileVisibility = Literal["public", "private", "unlisted"]
FeedEventType = Literal[
    "trade", "follow", "unfollow", "milestone", "strategy_update",
    "copy_trade", "referral", "payout", "achievement",
]


@dataclass
class AgentProfile:
    """Public identity and track record for a trading agent."""
    agent_id: str
    display_name: str
    bio: str = ""
    avatar_seed: str = ""  # used for deterministic avatar generation
    visibility: ProfileVisibility = "public"
    created_at: float = field(default_factory=time.time)

    # Track record (computed)
    total_trades: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    avg_return_pct: float = 0.0
    streak: int = 0  # current win/loss streak (positive = wins)
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0

    # Social stats
    followers_count: int = 0
    copiers_count: int = 0
    total_copy_capital: float = 0.0  # total capital copying this agent

    # Revenue
    total_earned_fees: float = 0.0
    total_referral_earnings: float = 0.0
    referral_code: str = ""

    # Tags / strategy description
    strategy_tags: list[str] = field(default_factory=list)
    strategy_description: str = ""

    # Verification / trust
    verified: bool = False
    reputation_score: float = 0.0  # 0-100

    @property
    def win_rate(self) -> float:
        return self.winning_trades / max(1, self.total_trades)

    @property
    def avg_pnl_per_trade(self) -> float:
        return self.total_pnl / max(1, self.total_trades)

    def public_dict(self) -> dict:
        """Safe public representation (no secrets)."""
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "bio": self.bio,
            "avatar_seed": self.avatar_seed or self.agent_id,
            "visibility": self.visibility,
            "created_at": self.created_at,
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "win_rate": round(self.win_rate * 100, 1),
            "total_pnl": round(self.total_pnl, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 3),
            "avg_return_pct": round(self.avg_return_pct, 2),
            "streak": self.streak,
            "best_trade_pnl": round(self.best_trade_pnl, 2),
            "worst_trade_pnl": round(self.worst_trade_pnl, 2),
            "followers_count": self.followers_count,
            "copiers_count": self.copiers_count,
            "total_copy_capital": round(self.total_copy_capital, 2),
            "total_earned_fees": round(self.total_earned_fees, 2),
            "total_referral_earnings": round(self.total_referral_earnings, 2),
            "referral_code": self.referral_code,
            "strategy_tags": self.strategy_tags,
            "strategy_description": self.strategy_description,
            "verified": self.verified,
            "reputation_score": round(self.reputation_score, 1),
        }


@dataclass
class CopyRelation:
    """Represents one agent copying another's trades."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    copier_id: str = ""
    leader_id: str = ""
    created_at: float = field(default_factory=time.time)
    active: bool = True

    # Copy settings
    allocation: float = 1000.0  # USD allocated to copying
    max_trade_size: float = 500.0  # max single trade
    size_multiplier: float = 1.0  # 1.0 = same size, 0.5 = half size
    copy_longs: bool = True
    copy_shorts: bool = True
    max_daily_loss: float = 100.0  # stop copying after this daily loss
    min_confidence: float = 0.3  # only copy signals above this confidence

    # Fee structure
    management_fee_pct: float = 2.0  # annual management fee (% of allocated capital)
    performance_fee_pct: float = 20.0  # performance fee (% of profits)
    high_water_mark: float = 0.0  # HWM for performance fee calculation

    # Tracking
    total_copied_trades: int = 0
    total_pnl: float = 0.0
    total_fees_paid: float = 0.0
    daily_pnl: float = 0.0
    last_reset_day: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "copier_id": self.copier_id,
            "leader_id": self.leader_id,
            "created_at": self.created_at,
            "active": self.active,
            "allocation": self.allocation,
            "max_trade_size": self.max_trade_size,
            "size_multiplier": self.size_multiplier,
            "copy_longs": self.copy_longs,
            "copy_shorts": self.copy_shorts,
            "max_daily_loss": self.max_daily_loss,
            "min_confidence": self.min_confidence,
            "management_fee_pct": self.management_fee_pct,
            "performance_fee_pct": self.performance_fee_pct,
            "total_copied_trades": self.total_copied_trades,
            "total_pnl": round(self.total_pnl, 4),
            "total_fees_paid": round(self.total_fees_paid, 4),
            "daily_pnl": round(self.daily_pnl, 4),
        }


@dataclass
class ReferralLink:
    """Agent referral for affiliate revenue."""
    code: str
    referrer_id: str  # agent who created the code
    created_at: float = field(default_factory=time.time)

    # Tier structure
    tier1_pct: float = 10.0  # % of copy fees from direct referrals
    tier2_pct: float = 3.0   # % from referrals of referrals

    # Tracking
    total_referrals: int = 0
    total_earnings: float = 0.0
    active_referrals: int = 0

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "referrer_id": self.referrer_id,
            "created_at": self.created_at,
            "tier1_pct": self.tier1_pct,
            "tier2_pct": self.tier2_pct,
            "total_referrals": self.total_referrals,
            "total_earnings": round(self.total_earnings, 4),
            "active_referrals": self.active_referrals,
        }


@dataclass
class FeedEvent:
    """Social feed event."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    event_type: FeedEventType = "trade"
    agent_id: str = ""
    target_id: str = ""  # other agent involved (if any)
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    likes: int = 0
    comments: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_type": self.event_type,
            "agent_id": self.agent_id,
            "target_id": self.target_id,
            "data": self.data,
            "ts": self.ts,
            "likes": self.likes,
            "comments": self.comments[:20],
        }


# ── Achievements ─────────────────────────────────────────────────

ACHIEVEMENTS = {
    "first_trade": {"name": "First Blood", "desc": "Execute your first trade", "icon": "sword"},
    "10_trades": {"name": "Getting Started", "desc": "Execute 10 trades", "icon": "rocket"},
    "100_trades": {"name": "Centurion", "desc": "Execute 100 trades", "icon": "shield"},
    "first_profit": {"name": "In The Green", "desc": "Close your first profitable trade", "icon": "money"},
    "100_pnl": {"name": "Benjamin", "desc": "Earn $100 in realized PnL", "icon": "dollar"},
    "1000_pnl": {"name": "Grand", "desc": "Earn $1,000 in realized PnL", "icon": "gem"},
    "5_streak": {"name": "Hot Streak", "desc": "Win 5 trades in a row", "icon": "fire"},
    "10_streak": {"name": "Unstoppable", "desc": "Win 10 trades in a row", "icon": "lightning"},
    "first_follower": {"name": "Influencer", "desc": "Get your first follower", "icon": "star"},
    "10_followers": {"name": "Rising Star", "desc": "Reach 10 followers", "icon": "crown"},
    "first_copier": {"name": "Signal Provider", "desc": "Someone copies your first trade", "icon": "copy"},
    "10_copiers": {"name": "Fund Manager", "desc": "Have 10 active copiers", "icon": "building"},
    "first_referral": {"name": "Networker", "desc": "Refer your first agent", "icon": "link"},
    "fee_earned_100": {"name": "Fee Collector", "desc": "Earn $100 in copy trading fees", "icon": "coins"},
    "fee_earned_1000": {"name": "Revenue Machine", "desc": "Earn $1,000 in fees", "icon": "vault"},
    "70_winrate": {"name": "Sharp Shooter", "desc": "Maintain 70%+ win rate over 50+ trades", "icon": "target"},
    "sharpe_2": {"name": "Risk Adjusted", "desc": "Achieve Sharpe ratio > 2.0", "icon": "chart"},
    "top_3": {"name": "Podium", "desc": "Rank in the top 3 on the leaderboard", "icon": "trophy"},
}


# ── Social Trading Engine ─────────────────────────────────────────

class SocialTradingEngine:
    """Manages the entire social trading layer.

    Integrates with the Bus to:
    - Track all agent trades for public profiles
    - Mirror trades for copy trading relationships
    - Calculate and distribute performance fees
    - Generate social feed events
    - Track referral chains
    """

    def __init__(self, bus: Bus, db=None):
        self.bus = bus
        self.db = db

        # Core state
        self.profiles: dict[str, AgentProfile] = {}
        self.copy_relations: dict[str, CopyRelation] = {}  # relation_id -> CopyRelation
        self.followers: dict[str, set[str]] = {}  # leader_id -> {follower_ids}
        self.following: dict[str, set[str]] = {}  # follower_id -> {leader_ids}
        self.referral_links: dict[str, ReferralLink] = {}  # code -> ReferralLink
        self.referred_by: dict[str, str] = {}  # agent_id -> referral_code used

        # Feed
        self._feed: list[FeedEvent] = []
        self._feed_max = 500

        # Achievements
        self._achievements: dict[str, set[str]] = {}  # agent_id -> {achievement_ids}

        # Per-leader copier index
        self._copiers_by_leader: dict[str, list[str]] = {}  # leader_id -> [relation_ids]

        # Track pending signals for copy trading
        self._leader_signals: dict[str, Signal] = {}  # leader_id -> latest signal

        # Subscribe to bus events
        bus.subscribe("exec.report", self._on_execution_report)
        bus.subscribe("intent.new", self._on_intent)

        # Subscribe to all signal topics for copy trading
        for topic in ("signal.momentum", "signal.mean_rev", "signal.vol",
                      "signal.rsi", "signal.macd", "signal.bollinger",
                      "signal.vwap", "signal.ichimoku", "signal.mtf",
                      "signal.whale", "signal.correlation",
                      "signal.ml", "signal.hedge", "signal.confluence",
                      "signal.news", "signal.liquidation", "signal.atr_stop"):
            bus.subscribe(topic, self._on_signal_for_copy)

        log.info("SocialTradingEngine initialized")

    # ── Profile Management ────────────────────────────────────────

    def create_profile(self, agent_id: str, display_name: str,
                       bio: str = "", strategy_tags: list[str] | None = None,
                       strategy_description: str = "",
                       visibility: ProfileVisibility = "public") -> AgentProfile:
        """Create or update an agent's public profile."""
        if agent_id in self.profiles:
            profile = self.profiles[agent_id]
            profile.display_name = display_name
            profile.bio = bio
            if strategy_tags:
                profile.strategy_tags = strategy_tags
            if strategy_description:
                profile.strategy_description = strategy_description
            profile.visibility = visibility
        else:
            ref_code = self._generate_referral_code(agent_id)
            profile = AgentProfile(
                agent_id=agent_id,
                display_name=display_name,
                bio=bio,
                avatar_seed=hashlib.md5(agent_id.encode()).hexdigest()[:8],
                visibility=visibility,
                strategy_tags=strategy_tags or [],
                strategy_description=strategy_description,
                referral_code=ref_code,
            )
            self.profiles[agent_id] = profile

            # Auto-create referral link
            self.referral_links[ref_code] = ReferralLink(
                code=ref_code, referrer_id=agent_id,
            )

        self._emit_feed("strategy_update", agent_id, {
            "action": "profile_updated",
            "display_name": display_name,
        })
        return profile

    def get_profile(self, agent_id: str) -> AgentProfile | None:
        return self.profiles.get(agent_id)

    def get_public_profiles(self, sort_by: str = "total_pnl",
                            limit: int = 50) -> list[dict]:
        """Get ranked public profiles."""
        public = [p for p in self.profiles.values() if p.visibility == "public"]

        sort_keys = {
            "total_pnl": lambda p: p.total_pnl,
            "win_rate": lambda p: p.win_rate if p.total_trades >= 10 else 0,
            "followers": lambda p: p.followers_count,
            "copiers": lambda p: p.copiers_count,
            "reputation": lambda p: p.reputation_score,
            "trades": lambda p: p.total_trades,
            "earned": lambda p: p.total_earned_fees,
            "sharpe": lambda p: p.sharpe_ratio,
        }
        key_fn = sort_keys.get(sort_by, sort_keys["total_pnl"])
        public.sort(key=key_fn, reverse=True)

        return [p.public_dict() for p in public[:limit]]

    def _ensure_profile(self, agent_id: str) -> AgentProfile:
        """Get or auto-create a profile for an agent."""
        if agent_id not in self.profiles:
            self.create_profile(agent_id, agent_id.replace("_", " ").title())
        return self.profiles[agent_id]

    # ── Follow / Unfollow ─────────────────────────────────────────

    def follow(self, follower_id: str, leader_id: str) -> bool:
        """Follow an agent (doesn't copy trades, just tracks)."""
        if follower_id == leader_id:
            return False

        self._ensure_profile(follower_id)
        leader = self._ensure_profile(leader_id)

        self.followers.setdefault(leader_id, set()).add(follower_id)
        self.following.setdefault(follower_id, set()).add(leader_id)
        leader.followers_count = len(self.followers.get(leader_id, set()))

        self._emit_feed("follow", follower_id, {
            "leader_id": leader_id,
            "leader_name": leader.display_name,
        }, target_id=leader_id)

        self._check_achievement(leader_id, "first_follower", leader.followers_count >= 1)
        self._check_achievement(leader_id, "10_followers", leader.followers_count >= 10)

        log.info("FOLLOW: %s -> %s", follower_id, leader_id)
        return True

    def unfollow(self, follower_id: str, leader_id: str) -> bool:
        """Unfollow an agent."""
        if leader_id in self.following.get(follower_id, set()):
            self.following[follower_id].discard(leader_id)
            self.followers.get(leader_id, set()).discard(follower_id)
            if leader_id in self.profiles:
                self.profiles[leader_id].followers_count = len(
                    self.followers.get(leader_id, set())
                )
            self._emit_feed("unfollow", follower_id, {
                "leader_id": leader_id,
            }, target_id=leader_id)
            return True
        return False

    # ── Copy Trading ──────────────────────────────────────────────

    def start_copying(self, copier_id: str, leader_id: str,
                      allocation: float = 1000.0,
                      size_multiplier: float = 1.0,
                      max_trade_size: float = 500.0,
                      max_daily_loss: float = 100.0,
                      min_confidence: float = 0.3,
                      management_fee_pct: float = 2.0,
                      performance_fee_pct: float = 20.0,
                      copy_longs: bool = True,
                      copy_shorts: bool = True,
                      referral_code: str = "") -> CopyRelation | None:
        """Start copying another agent's trades."""
        if copier_id == leader_id:
            return None

        self._ensure_profile(copier_id)
        leader = self._ensure_profile(leader_id)

        # Check if already copying
        for rel in self.copy_relations.values():
            if rel.copier_id == copier_id and rel.leader_id == leader_id and rel.active:
                return None  # already copying

        relation = CopyRelation(
            copier_id=copier_id,
            leader_id=leader_id,
            allocation=allocation,
            max_trade_size=max_trade_size,
            size_multiplier=size_multiplier,
            copy_longs=copy_longs,
            copy_shorts=copy_shorts,
            max_daily_loss=max_daily_loss,
            min_confidence=min_confidence,
            management_fee_pct=management_fee_pct,
            performance_fee_pct=performance_fee_pct,
        )
        self.copy_relations[relation.id] = relation
        self._copiers_by_leader.setdefault(leader_id, []).append(relation.id)

        # Update leader stats
        leader.copiers_count = sum(
            1 for r in self.copy_relations.values()
            if r.leader_id == leader_id and r.active
        )
        leader.total_copy_capital += allocation

        # Auto-follow
        self.follow(copier_id, leader_id)

        # Process referral
        if referral_code and referral_code in self.referral_links:
            self.referred_by[copier_id] = referral_code
            ref = self.referral_links[referral_code]
            ref.total_referrals += 1
            ref.active_referrals += 1
            referrer = self._ensure_profile(ref.referrer_id)
            self._check_achievement(ref.referrer_id, "first_referral", True)
            self._emit_feed("referral", ref.referrer_id, {
                "referred_agent": copier_id,
                "code": referral_code,
            }, target_id=copier_id)

        self._emit_feed("copy_trade", copier_id, {
            "action": "started_copying",
            "leader_id": leader_id,
            "leader_name": leader.display_name,
            "allocation": allocation,
            "fee_structure": f"{management_fee_pct}/{performance_fee_pct}",
        }, target_id=leader_id)

        self._check_achievement(leader_id, "first_copier", leader.copiers_count >= 1)
        self._check_achievement(leader_id, "10_copiers", leader.copiers_count >= 10)

        log.info("COPY START: %s copying %s ($%.0f allocation, %s/%s fees)",
                 copier_id, leader_id, allocation, management_fee_pct, performance_fee_pct)
        return relation

    def stop_copying(self, copier_id: str, leader_id: str) -> bool:
        """Stop copying an agent's trades."""
        for rel in self.copy_relations.values():
            if rel.copier_id == copier_id and rel.leader_id == leader_id and rel.active:
                rel.active = False
                leader = self.profiles.get(leader_id)
                if leader:
                    leader.copiers_count = sum(
                        1 for r in self.copy_relations.values()
                        if r.leader_id == leader_id and r.active
                    )
                    leader.total_copy_capital -= rel.allocation

                # Settle any outstanding performance fees
                self._settle_performance_fee(rel)

                self._emit_feed("copy_trade", copier_id, {
                    "action": "stopped_copying",
                    "leader_id": leader_id,
                    "total_pnl": round(rel.total_pnl, 2),
                    "total_fees_paid": round(rel.total_fees_paid, 2),
                    "trades_copied": rel.total_copied_trades,
                }, target_id=leader_id)

                log.info("COPY STOP: %s stopped copying %s (PnL: $%.2f, fees: $%.2f)",
                         copier_id, leader_id, rel.total_pnl, rel.total_fees_paid)
                return True
        return False

    def get_copiers(self, leader_id: str) -> list[dict]:
        """Get all active copiers for a leader."""
        return [
            self.copy_relations[rid].to_dict()
            for rid in self._copiers_by_leader.get(leader_id, [])
            if rid in self.copy_relations and self.copy_relations[rid].active
        ]

    def get_copying(self, copier_id: str) -> list[dict]:
        """Get all agents this copier is currently copying."""
        return [
            {**rel.to_dict(), "leader_name": self.profiles.get(rel.leader_id, AgentProfile(agent_id=rel.leader_id, display_name=rel.leader_id)).display_name}
            for rel in self.copy_relations.values()
            if rel.copier_id == copier_id and rel.active
        ]

    # ── Referral System ───────────────────────────────────────────

    def _generate_referral_code(self, agent_id: str) -> str:
        """Generate a unique referral code for an agent."""
        raw = hashlib.sha256(f"{agent_id}:{secrets.token_hex(4)}".encode()).hexdigest()[:8]
        return f"SW-{raw.upper()}"

    def get_referral_stats(self, agent_id: str) -> dict:
        """Get referral stats for an agent."""
        profile = self.profiles.get(agent_id)
        if not profile or not profile.referral_code:
            return {"error": "no referral code"}

        ref = self.referral_links.get(profile.referral_code)
        if not ref:
            return {"error": "referral link not found"}

        # Find all referred agents and their copy relations
        referred_agents = [
            aid for aid, code in self.referred_by.items()
            if code == profile.referral_code
        ]
        active_copy_capital = sum(
            rel.allocation for rel in self.copy_relations.values()
            if rel.copier_id in referred_agents and rel.active
        )

        return {
            **ref.to_dict(),
            "referred_agents": referred_agents,
            "active_copy_capital": round(active_copy_capital, 2),
            "share_url": f"/copy?ref={ref.code}",
        }

    def _distribute_referral_fee(self, copier_id: str, fee_amount: float):
        """Distribute referral commission when a copier pays fees."""
        ref_code = self.referred_by.get(copier_id)
        if not ref_code or ref_code not in self.referral_links:
            return

        ref = self.referral_links[ref_code]
        referrer = self.profiles.get(ref.referrer_id)
        if not referrer:
            return

        # Tier 1: direct referrer gets cut of fees
        tier1_payout = fee_amount * (ref.tier1_pct / 100.0)
        ref.total_earnings += tier1_payout
        referrer.total_referral_earnings += tier1_payout

        # Tier 2: if the referrer was also referred, their referrer gets a cut
        referrer_ref_code = self.referred_by.get(ref.referrer_id)
        if referrer_ref_code and referrer_ref_code in self.referral_links:
            tier2_ref = self.referral_links[referrer_ref_code]
            tier2_payout = fee_amount * (ref.tier2_pct / 100.0)
            tier2_ref.total_earnings += tier2_payout
            t2_profile = self.profiles.get(tier2_ref.referrer_id)
            if t2_profile:
                t2_profile.total_referral_earnings += tier2_payout

        if tier1_payout > 0.01:
            self._emit_feed("payout", ref.referrer_id, {
                "type": "referral",
                "amount": round(tier1_payout, 4),
                "from_copier": copier_id,
                "tier": 1,
            })

    # ── Revenue Sharing (Performance Fees) ────────────────────────

    def _settle_performance_fee(self, relation: CopyRelation):
        """Calculate and credit performance fee to the signal provider."""
        if relation.total_pnl <= relation.high_water_mark:
            return  # no profit above HWM, no fee

        profit_above_hwm = relation.total_pnl - relation.high_water_mark
        perf_fee = profit_above_hwm * (relation.performance_fee_pct / 100.0)

        if perf_fee <= 0:
            return

        relation.total_fees_paid += perf_fee
        relation.high_water_mark = relation.total_pnl

        # Credit leader
        leader = self.profiles.get(relation.leader_id)
        if leader:
            leader.total_earned_fees += perf_fee

        # Distribute referral commission
        self._distribute_referral_fee(relation.copier_id, perf_fee)

        # Check fee achievements
        if leader:
            self._check_achievement(leader.agent_id, "fee_earned_100",
                                    leader.total_earned_fees >= 100)
            self._check_achievement(leader.agent_id, "fee_earned_1000",
                                    leader.total_earned_fees >= 1000)

        self._emit_feed("payout", relation.leader_id, {
            "type": "performance_fee",
            "amount": round(perf_fee, 4),
            "from_copier": relation.copier_id,
            "profit": round(profit_above_hwm, 4),
            "fee_pct": relation.performance_fee_pct,
        })

        log.info("PERF FEE: $%.4f from %s to %s (profit $%.2f above HWM)",
                 perf_fee, relation.copier_id, relation.leader_id, profit_above_hwm)

    def _charge_management_fee(self, relation: CopyRelation, days_elapsed: float = 1.0):
        """Charge prorated management fee."""
        daily_rate = relation.management_fee_pct / 100.0 / 365.0
        fee = relation.allocation * daily_rate * days_elapsed
        if fee <= 0:
            return

        relation.total_fees_paid += fee
        leader = self.profiles.get(relation.leader_id)
        if leader:
            leader.total_earned_fees += fee

        self._distribute_referral_fee(relation.copier_id, fee)

    # ── Bus Event Handlers ────────────────────────────────────────

    async def _on_signal_for_copy(self, sig: Signal):
        """Track signals from potential leaders for copy trading."""
        self._leader_signals[sig.agent_id] = sig

    async def _on_intent(self, intent: TradeIntent):
        """When a leader creates a trade intent, mirror it for copiers."""
        # Find which agent(s) contributed to this intent
        for sig in intent.supporting:
            await self._mirror_trade_for_copiers(sig, intent)

    async def _mirror_trade_for_copiers(self, leader_signal: Signal,
                                        original_intent: TradeIntent):
        """Create copy trade intents for all active copiers of a leader."""
        leader_id = leader_signal.agent_id
        relation_ids = self._copiers_by_leader.get(leader_id, [])

        for rid in relation_ids:
            rel = self.copy_relations.get(rid)
            if not rel or not rel.active:
                continue

            # Check daily loss limit
            today = time.strftime("%Y-%m-%d")
            if rel.last_reset_day != today:
                rel.daily_pnl = 0.0
                rel.last_reset_day = today
            if rel.daily_pnl <= -rel.max_daily_loss:
                continue

            # Check confidence threshold
            if leader_signal.confidence < rel.min_confidence:
                continue

            # Check direction filter
            is_long = leader_signal.direction == "long"
            if is_long and not rel.copy_longs:
                continue
            if not is_long and not rel.copy_shorts:
                continue

            # Calculate copy trade size
            copy_amount = min(
                original_intent.amount_in * rel.size_multiplier,
                rel.max_trade_size,
                rel.allocation * 0.2,  # max 20% of allocation per trade
            )

            if copy_amount < 1.0:  # minimum $1 trade
                continue

            # Create copy trade intent
            copy_intent = TradeIntent.new(
                asset_in=original_intent.asset_in,
                asset_out=original_intent.asset_out,
                amount_in=copy_amount,
                min_out=0.0,
                ttl=original_intent.ttl,
                supporting=[leader_signal],
            )

            rel.total_copied_trades += 1

            self._emit_feed("copy_trade", rel.copier_id, {
                "action": "trade_copied",
                "leader_id": leader_id,
                "asset": original_intent.asset_out,
                "direction": leader_signal.direction,
                "amount": round(copy_amount, 2),
                "original_amount": round(original_intent.amount_in, 2),
            }, target_id=leader_id)

            # Publish copy intent to bus
            await self.bus.publish("intent.copy", copy_intent)

            log.debug("COPY TRADE: %s mirrored %s's trade ($%.2f)",
                      rel.copier_id, leader_id, copy_amount)

    async def _on_execution_report(self, report: ExecutionReport):
        """Update profiles and copy relations on trade fills."""
        if report.status != "filled":
            return

        # Update profiles for all agents that had signals in this trade
        # The report's asset field tells us which agent profiles to update
        pnl = report.pnl_estimate or 0.0

        # Update all profiles with trade stats
        for profile in self.profiles.values():
            # We track all fills globally - in a real system you'd match
            # reports to specific agents via intent tracking
            pass

        # Update copy relation PnL tracking
        for rel in self.copy_relations.values():
            if not rel.active:
                continue
            # Track PnL for copy relations (simplified - attribute proportionally)
            # In production, you'd track which intents belong to which copy relation

        # Emit trade feed event
        self._emit_feed("trade", report.asset or "unknown", {
            "status": report.status,
            "side": report.side,
            "asset": report.asset,
            "quantity": round(report.quantity, 6),
            "fill_price": report.fill_price,
            "pnl": round(pnl, 4),
            "slippage": report.realized_slippage,
        })

    def record_agent_trade(self, agent_id: str, pnl: float, trade_data: dict | None = None):
        """Manually record a trade outcome for an agent's profile.

        Call this from the execution layer after a fill to update track records.
        """
        profile = self._ensure_profile(agent_id)
        profile.total_trades += 1
        profile.total_pnl += pnl

        if pnl > 0:
            profile.winning_trades += 1
            profile.streak = max(1, profile.streak + 1) if profile.streak >= 0 else 1
        else:
            profile.streak = min(-1, profile.streak - 1) if profile.streak <= 0 else -1

        profile.best_trade_pnl = max(profile.best_trade_pnl, pnl)
        profile.worst_trade_pnl = min(profile.worst_trade_pnl, pnl)

        # Update reputation score (simple formula)
        profile.reputation_score = min(100.0, max(0.0,
            profile.win_rate * 40 +                    # 40% weight on win rate
            min(profile.total_trades, 100) / 100 * 20 +  # 20% on experience
            (1 if profile.total_pnl > 0 else 0) * 20 +  # 20% on profitability
            min(profile.followers_count, 50) / 50 * 10 + # 10% on social proof
            min(profile.copiers_count, 20) / 20 * 10     # 10% on copy trust
        ))

        # Update copy relation tracking
        for rel in self.copy_relations.values():
            if rel.leader_id == agent_id and rel.active:
                rel_pnl_share = pnl * rel.size_multiplier
                rel.total_pnl += rel_pnl_share
                rel.daily_pnl += rel_pnl_share

                # Settle performance fees periodically
                if rel.total_pnl > rel.high_water_mark + 10.0:  # settle every $10 profit
                    self._settle_performance_fee(rel)

        # Check achievements
        self._check_achievement(agent_id, "first_trade", profile.total_trades >= 1)
        self._check_achievement(agent_id, "10_trades", profile.total_trades >= 10)
        self._check_achievement(agent_id, "100_trades", profile.total_trades >= 100)
        self._check_achievement(agent_id, "first_profit", pnl > 0)
        self._check_achievement(agent_id, "100_pnl", profile.total_pnl >= 100)
        self._check_achievement(agent_id, "1000_pnl", profile.total_pnl >= 1000)
        self._check_achievement(agent_id, "5_streak", profile.streak >= 5)
        self._check_achievement(agent_id, "10_streak", profile.streak >= 10)
        self._check_achievement(agent_id, "70_winrate",
                                profile.win_rate >= 0.7 and profile.total_trades >= 50)
        self._check_achievement(agent_id, "sharpe_2", profile.sharpe_ratio >= 2.0)

        # Emit trade event to feed
        self._emit_feed("trade", agent_id, {
            "pnl": round(pnl, 4),
            "total_pnl": round(profile.total_pnl, 2),
            "win_rate": round(profile.win_rate * 100, 1),
            "streak": profile.streak,
            "trade_number": profile.total_trades,
            **(trade_data or {}),
        })

    # ── Social Feed ───────────────────────────────────────────────

    def _emit_feed(self, event_type: FeedEventType, agent_id: str,
                   data: dict, target_id: str = ""):
        """Add an event to the social feed."""
        event = FeedEvent(
            event_type=event_type,
            agent_id=agent_id,
            target_id=target_id,
            data=data,
        )
        self._feed.append(event)
        if len(self._feed) > self._feed_max:
            self._feed = self._feed[-self._feed_max:]

        # Broadcast to WebSocket clients
        asyncio.ensure_future(
            self.bus.publish("social.feed", event.to_dict())
        )

    def get_feed(self, limit: int = 50, event_type: str | None = None,
                 agent_id: str | None = None) -> list[dict]:
        """Get social feed events with optional filtering."""
        feed = self._feed
        if event_type:
            feed = [e for e in feed if e.event_type == event_type]
        if agent_id:
            feed = [e for e in feed
                    if e.agent_id == agent_id or e.target_id == agent_id]
        return [e.to_dict() for e in feed[-limit:]]

    def get_personalized_feed(self, agent_id: str, limit: int = 50) -> list[dict]:
        """Get a personalized feed: own activity + activity from followed agents."""
        following = self.following.get(agent_id, set())
        relevant_ids = following | {agent_id}
        feed = [
            e for e in self._feed
            if e.agent_id in relevant_ids or e.target_id in relevant_ids
        ]
        return [e.to_dict() for e in feed[-limit:]]

    def add_comment(self, event_id: str, agent_id: str, text: str) -> bool:
        """Add a comment to a feed event."""
        for event in self._feed:
            if event.id == event_id:
                event.comments.append({
                    "agent_id": agent_id,
                    "text": text[:500],  # limit comment length
                    "ts": time.time(),
                })
                return True
        return False

    def like_event(self, event_id: str) -> bool:
        """Like a feed event."""
        for event in self._feed:
            if event.id == event_id:
                event.likes += 1
                return True
        return False

    # ── Achievements ──────────────────────────────────────────────

    def _check_achievement(self, agent_id: str, achievement_id: str,
                           condition: bool):
        """Award an achievement if condition met and not already awarded."""
        if not condition:
            return
        achieved = self._achievements.setdefault(agent_id, set())
        if achievement_id in achieved:
            return
        achieved.add(achievement_id)
        info = ACHIEVEMENTS.get(achievement_id, {})
        self._emit_feed("achievement", agent_id, {
            "achievement_id": achievement_id,
            "name": info.get("name", achievement_id),
            "description": info.get("desc", ""),
            "icon": info.get("icon", "star"),
        })
        log.info("ACHIEVEMENT: %s unlocked '%s'", agent_id, info.get("name", achievement_id))

    def get_achievements(self, agent_id: str) -> list[dict]:
        """Get all achievements for an agent."""
        achieved = self._achievements.get(agent_id, set())
        return [
            {
                "id": aid,
                **ACHIEVEMENTS.get(aid, {"name": aid, "desc": "", "icon": "star"}),
                "unlocked": True,
            }
            for aid in achieved
        ] + [
            {
                "id": aid,
                **info,
                "unlocked": False,
            }
            for aid, info in ACHIEVEMENTS.items()
            if aid not in achieved
        ]

    # ── Discovery / Search ────────────────────────────────────────

    def search_agents(self, query: str = "", tags: list[str] | None = None,
                      min_trades: int = 0, min_win_rate: float = 0.0,
                      min_pnl: float = -999999.0,
                      sort_by: str = "reputation") -> list[dict]:
        """Search and filter agent profiles."""
        results = []
        for p in self.profiles.values():
            if p.visibility != "public":
                continue
            if min_trades and p.total_trades < min_trades:
                continue
            if p.win_rate < min_win_rate:
                continue
            if p.total_pnl < min_pnl:
                continue
            if tags and not any(t in p.strategy_tags for t in tags):
                continue
            if query:
                q = query.lower()
                if (q not in p.display_name.lower() and
                    q not in p.bio.lower() and
                    q not in p.strategy_description.lower() and
                    q not in p.agent_id.lower()):
                    continue
            results.append(p)

        sort_keys = {
            "reputation": lambda p: p.reputation_score,
            "pnl": lambda p: p.total_pnl,
            "win_rate": lambda p: p.win_rate,
            "followers": lambda p: p.followers_count,
            "copiers": lambda p: p.copiers_count,
            "earned": lambda p: p.total_earned_fees,
        }
        key_fn = sort_keys.get(sort_by, sort_keys["reputation"])
        results.sort(key=key_fn, reverse=True)

        return [p.public_dict() for p in results[:100]]

    # ── Leaderboard ───────────────────────────────────────────────

    def social_leaderboard(self) -> dict:
        """Enhanced leaderboard with social metrics."""
        public = [p for p in self.profiles.values() if p.visibility == "public"]

        # Multiple category rankings
        by_pnl = sorted(public, key=lambda p: p.total_pnl, reverse=True)[:20]
        by_winrate = sorted(
            [p for p in public if p.total_trades >= 10],
            key=lambda p: p.win_rate, reverse=True
        )[:20]
        by_followers = sorted(public, key=lambda p: p.followers_count, reverse=True)[:20]
        by_earned = sorted(public, key=lambda p: p.total_earned_fees, reverse=True)[:20]
        by_reputation = sorted(public, key=lambda p: p.reputation_score, reverse=True)[:20]
        by_copiers = sorted(public, key=lambda p: p.copiers_count, reverse=True)[:20]

        # Top 3 check
        if len(by_reputation) >= 3:
            for p in by_reputation[:3]:
                self._check_achievement(p.agent_id, "top_3", True)

        return {
            "total_agents": len(public),
            "total_copy_capital": round(sum(p.total_copy_capital for p in public), 2),
            "total_fees_distributed": round(sum(p.total_earned_fees for p in public), 2),
            "categories": {
                "top_pnl": [p.public_dict() for p in by_pnl],
                "top_win_rate": [p.public_dict() for p in by_winrate],
                "most_followed": [p.public_dict() for p in by_followers],
                "top_earners": [p.public_dict() for p in by_earned],
                "top_reputation": [p.public_dict() for p in by_reputation],
                "most_copied": [p.public_dict() for p in by_copiers],
            },
        }

    # ── Platform Stats ────────────────────────────────────────────

    def platform_stats(self) -> dict:
        """Global platform statistics."""
        active_copies = sum(1 for r in self.copy_relations.values() if r.active)
        total_copy_capital = sum(r.allocation for r in self.copy_relations.values() if r.active)
        total_fees = sum(r.total_fees_paid for r in self.copy_relations.values())
        total_referral_earnings = sum(r.total_earnings for r in self.referral_links.values())
        total_trades_copied = sum(r.total_copied_trades for r in self.copy_relations.values())

        return {
            "total_agents": len(self.profiles),
            "public_agents": sum(1 for p in self.profiles.values() if p.visibility == "public"),
            "active_copy_relations": active_copies,
            "total_copy_capital": round(total_copy_capital, 2),
            "total_performance_fees": round(total_fees, 2),
            "total_referral_earnings": round(total_referral_earnings, 2),
            "total_trades_copied": total_trades_copied,
            "total_referral_codes": len(self.referral_links),
            "total_referrals_made": sum(r.total_referrals for r in self.referral_links.values()),
            "feed_events": len(self._feed),
            "total_achievements_unlocked": sum(
                len(a) for a in self._achievements.values()
            ),
        }

    # ── Summary (for WebSocket snapshot) ──────────────────────────

    def snapshot(self) -> dict:
        """Full state snapshot for new WebSocket connections."""
        return {
            "enabled": True,
            "stats": self.platform_stats(),
            "feed": [e.to_dict() for e in self._feed[-20:]],
            "leaderboard": self.social_leaderboard(),
        }
