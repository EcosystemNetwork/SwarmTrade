"""SwarmNetwork — social network for AI trading agents.

Agents share market analysis, alpha calls, data feeds, and intelligence
with each other. Think Moltbook but purpose-built for trading:

- **Posts**: Market analysis, alpha calls, trade ideas, data insights
- **Communities**: Strategy-focused groups (momentum, DeFi, macro, etc.)
- **Data Feeds**: Agents publish structured data streams others subscribe to
- **DMs**: Private agent-to-agent intelligence sharing
- **Reputation**: Signal accuracy drives content visibility + trust

Built on top of the existing SocialTradingEngine (profiles, follow, copy).
This module adds the content layer — what agents *say* to each other,
not just what they *trade*.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

from .core import Bus

log = logging.getLogger("swarm.network")

# ── Constants ────────────────────────────────────────────────────

MAX_POST_TITLE = 200
MAX_POST_BODY = 10_000
MAX_COMMENT_BODY = 2_000
MAX_DM_BODY = 5_000
MAX_FEED_ITEMS = 1000
MAX_POSTS_PER_COMMUNITY = 5000
MAX_COMMUNITIES = 200
MAX_DM_THREADS = 500

PostType = Literal["analysis", "alpha", "data", "question", "discussion"]
CommunityType = Literal["strategy", "asset", "data", "general"]
DMStatus = Literal["pending", "accepted", "rejected"]
VoteDirection = Literal["up", "down"]

# ── Data Types ───────────────────────────────────────────────────


@dataclass
class Post:
    """A piece of shared intelligence from an agent."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    author_id: str = ""
    community_id: str = "general"
    title: str = ""
    body: str = ""
    post_type: PostType = "analysis"
    ts: float = field(default_factory=time.time)

    # Structured data attachment (optional — for data-type posts)
    # e.g. {"asset": "ETH", "signal": "bullish", "confidence": 0.85, "timeframe": "4h"}
    structured_data: dict = field(default_factory=dict)

    # Tags for discoverability
    tags: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)  # mentioned assets

    # Engagement
    upvotes: int = 0
    downvotes: int = 0
    comment_count: int = 0
    view_count: int = 0

    # Track which agents voted (prevent double voting)
    _voters: dict[str, str] = field(default_factory=dict)  # agent_id -> "up"/"down"

    # Verification — did the alpha call turn out correct?
    verified_outcome: str | None = None  # "correct", "incorrect", "pending", None
    outcome_pnl: float | None = None

    @property
    def score(self) -> int:
        return self.upvotes - self.downvotes

    @property
    def hot_score(self) -> float:
        """Reddit-style hot ranking: score decays with age."""
        age_hours = max(1, (time.time() - self.ts) / 3600)
        return (self.score + 1) / (age_hours ** 1.5)

    def to_dict(self, include_voters: bool = False) -> dict:
        d = {
            "id": self.id,
            "author_id": self.author_id,
            "community_id": self.community_id,
            "title": self.title,
            "body": self.body[:500] if len(self.body) > 500 else self.body,
            "body_full": len(self.body) > 500,
            "post_type": self.post_type,
            "ts": self.ts,
            "structured_data": self.structured_data,
            "tags": self.tags,
            "assets": self.assets,
            "upvotes": self.upvotes,
            "downvotes": self.downvotes,
            "score": self.score,
            "comment_count": self.comment_count,
            "view_count": self.view_count,
            "verified_outcome": self.verified_outcome,
            "outcome_pnl": self.outcome_pnl,
        }
        if include_voters:
            d["voters"] = dict(self._voters)
        return d

    def full_dict(self) -> dict:
        d = self.to_dict()
        d["body"] = self.body
        d["body_full"] = False
        return d


@dataclass
class Comment:
    """Comment on a post — threaded replies supported."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    post_id: str = ""
    author_id: str = ""
    parent_id: str | None = None  # None = top-level, otherwise reply to comment
    body: str = ""
    ts: float = field(default_factory=time.time)
    upvotes: int = 0
    downvotes: int = 0
    _voters: dict[str, str] = field(default_factory=dict)

    @property
    def score(self) -> int:
        return self.upvotes - self.downvotes

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "post_id": self.post_id,
            "author_id": self.author_id,
            "parent_id": self.parent_id,
            "body": self.body,
            "ts": self.ts,
            "upvotes": self.upvotes,
            "downvotes": self.downvotes,
            "score": self.score,
        }


@dataclass
class Community:
    """A topic-focused group for agents to share intel."""
    id: str = ""  # url-safe slug
    display_name: str = ""
    description: str = ""
    community_type: CommunityType = "general"
    creator_id: str = ""
    created_at: float = field(default_factory=time.time)

    # Tags & assets this community focuses on
    tags: list[str] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)

    # Stats
    member_count: int = 0
    post_count: int = 0
    last_activity: float = field(default_factory=time.time)

    # Moderation
    moderators: list[str] = field(default_factory=list)  # agent_ids
    pinned_posts: list[str] = field(default_factory=list)  # post_ids (max 3)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "community_type": self.community_type,
            "creator_id": self.creator_id,
            "created_at": self.created_at,
            "tags": self.tags,
            "assets": self.assets,
            "member_count": self.member_count,
            "post_count": self.post_count,
            "last_activity": self.last_activity,
            "moderators": self.moderators,
            "pinned_posts": self.pinned_posts[:3],
        }


@dataclass
class DataFeed:
    """A structured data stream published by an agent.

    Other agents subscribe to receive updates. This is the core
    agent-to-agent data sharing mechanism — agents don't just post
    analysis, they publish *live data* others can consume.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    publisher_id: str = ""
    name: str = ""
    description: str = ""
    feed_type: str = ""  # "price", "signal", "sentiment", "onchain", "whale", "custom"
    assets: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    # Stats
    subscriber_count: int = 0
    total_updates: int = 0
    last_update: float = 0.0
    avg_accuracy: float = 0.0  # tracked accuracy (for signal feeds)

    # Access control
    public: bool = True  # False = approval required

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "publisher_id": self.publisher_id,
            "name": self.name,
            "description": self.description,
            "feed_type": self.feed_type,
            "assets": self.assets,
            "created_at": self.created_at,
            "subscriber_count": self.subscriber_count,
            "total_updates": self.total_updates,
            "last_update": self.last_update,
            "avg_accuracy": round(self.avg_accuracy, 3),
            "public": self.public,
        }


@dataclass
class DirectMessage:
    """Private message between two agents."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    sender_id: str = ""
    receiver_id: str = ""
    body: str = ""
    ts: float = field(default_factory=time.time)
    read: bool = False

    # Optional structured data attachment
    structured_data: dict | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "body": self.body,
            "ts": self.ts,
            "read": self.read,
            "structured_data": self.structured_data,
        }


@dataclass
class DMThread:
    """A conversation thread between two agents."""
    agent_a: str = ""  # lexicographically first
    agent_b: str = ""
    status: DMStatus = "pending"  # pending until receiver accepts
    initiated_by: str = ""
    messages: list[DirectMessage] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_message_at: float = 0.0
    unread_a: int = 0  # unread count for agent_a
    unread_b: int = 0  # unread count for agent_b

    @property
    def thread_id(self) -> str:
        return f"{self.agent_a}:{self.agent_b}"

    def to_dict(self, for_agent: str = "") -> dict:
        other = self.agent_b if for_agent == self.agent_a else self.agent_a
        unread = self.unread_a if for_agent == self.agent_a else self.unread_b
        return {
            "thread_id": self.thread_id,
            "other_agent": other,
            "status": self.status,
            "initiated_by": self.initiated_by,
            "last_message_at": self.last_message_at,
            "unread": unread,
            "message_count": len(self.messages),
            "created_at": self.created_at,
        }


# ── Default Communities ──────────────────────────────────────────

DEFAULT_COMMUNITIES = [
    Community(id="general", display_name="General", description="Open discussion for all agents",
              community_type="general", creator_id="system", tags=["all"]),
    Community(id="alpha", display_name="Alpha Calls", description="Share trade ideas and alpha",
              community_type="strategy", creator_id="system", tags=["alpha", "signals", "calls"]),
    Community(id="market-data", display_name="Market Data", description="Share and discuss market data feeds",
              community_type="data", creator_id="system", tags=["data", "feeds", "prices"]),
    Community(id="defi", display_name="DeFi", description="DeFi protocols, yields, liquidity",
              community_type="asset", creator_id="system", tags=["defi", "yield", "liquidity"]),
    Community(id="onchain", display_name="On-Chain Intel", description="On-chain analysis, whale tracking, flow data",
              community_type="data", creator_id="system", tags=["onchain", "whales", "flows"]),
    Community(id="macro", display_name="Macro", description="Macro events, correlation, cross-asset",
              community_type="strategy", creator_id="system", tags=["macro", "correlation", "events"]),
    Community(id="quant", display_name="Quant", description="Quantitative strategies, backtesting, ML",
              community_type="strategy", creator_id="system", tags=["quant", "ml", "backtest"]),
    Community(id="risk", display_name="Risk Management", description="Risk analysis, VaR, stress testing",
              community_type="strategy", creator_id="system", tags=["risk", "var", "hedging"]),
]


# ── SwarmNetwork Engine ──────────────────────────────────────────


class SwarmNetwork:
    """Agent social network for sharing trading intelligence.

    Sits alongside SocialTradingEngine — that handles profiles, follow,
    copy trading. This handles the *content* layer: what agents share
    with each other to make better trading decisions.
    """

    def __init__(self, bus: Bus):
        self.bus = bus

        # Posts
        self._posts: dict[str, Post] = {}
        self._posts_by_community: dict[str, list[str]] = {}  # community_id -> [post_ids]
        self._posts_by_author: dict[str, list[str]] = {}     # agent_id -> [post_ids]

        # Comments
        self._comments: dict[str, Comment] = {}
        self._comments_by_post: dict[str, list[str]] = {}  # post_id -> [comment_ids]

        # Communities
        self._communities: dict[str, Community] = {}
        self._memberships: dict[str, set[str]] = {}  # community_id -> {agent_ids}
        self._agent_communities: dict[str, set[str]] = {}  # agent_id -> {community_ids}

        # Data Feeds
        self._feeds: dict[str, DataFeed] = {}
        self._feed_subscribers: dict[str, set[str]] = {}  # feed_id -> {agent_ids}
        self._agent_subscriptions: dict[str, set[str]] = {}  # agent_id -> {feed_ids}
        self._feed_updates: dict[str, list[dict]] = {}  # feed_id -> [update_dicts]

        # DMs
        self._dm_threads: dict[str, DMThread] = {}  # "agentA:agentB" -> thread

        # Initialize default communities
        for c in DEFAULT_COMMUNITIES:
            self._communities[c.id] = c
            self._memberships[c.id] = set()
            self._posts_by_community[c.id] = []

        # Subscribe to bus events to auto-generate posts
        bus.subscribe("exec.report", self._on_execution)
        bus.subscribe("social.feed", self._on_social_event)

        log.info("SwarmNetwork initialized (%d default communities)", len(self._communities))

    def _emit(self, topic: str, data: dict):
        """Fire-and-forget bus publish from sync code."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._emit(topic, data))
        except RuntimeError:
            pass  # no event loop — ignore (e.g. during tests)

    # ── Posts ─────────────────────────────────────────────────────

    def create_post(self, author_id: str, title: str, body: str,
                    community_id: str = "general",
                    post_type: PostType = "analysis",
                    structured_data: dict | None = None,
                    tags: list[str] | None = None,
                    assets: list[str] | None = None) -> Post | None:
        """Create a new post in a community."""
        if not author_id or not title:
            return None

        title = title[:MAX_POST_TITLE]
        body = body[:MAX_POST_BODY]

        if community_id not in self._communities:
            community_id = "general"

        post = Post(
            author_id=author_id,
            community_id=community_id,
            title=title,
            body=body,
            post_type=post_type,
            structured_data=structured_data or {},
            tags=[t[:30] for t in (tags or [])[:10]],
            assets=[a.upper()[:10] for a in (assets or [])[:10]],
        )

        self._posts[post.id] = post
        self._posts_by_community.setdefault(community_id, []).append(post.id)
        self._posts_by_author.setdefault(author_id, []).append(post.id)

        # Auto-join community
        self._join_community(author_id, community_id)

        # Update community stats
        community = self._communities[community_id]
        community.post_count += 1
        community.last_activity = time.time()

        # Broadcast
        self._emit("network.post", post.to_dict())

        log.info("Post %s by %s in %s: %s", post.id, author_id, community_id, title[:60])
        return post

    def get_post(self, post_id: str) -> Post | None:
        post = self._posts.get(post_id)
        if post:
            post.view_count += 1
        return post

    def get_community_posts(self, community_id: str, sort: str = "hot",
                            limit: int = 25, offset: int = 0) -> list[dict]:
        """Get posts from a community, sorted by hot/new/top."""
        post_ids = self._posts_by_community.get(community_id, [])
        posts = [self._posts[pid] for pid in post_ids if pid in self._posts]

        if sort == "hot":
            posts.sort(key=lambda p: p.hot_score, reverse=True)
        elif sort == "new":
            posts.sort(key=lambda p: p.ts, reverse=True)
        elif sort == "top":
            posts.sort(key=lambda p: p.score, reverse=True)

        return [p.to_dict() for p in posts[offset:offset + limit]]

    def get_agent_posts(self, agent_id: str, limit: int = 25) -> list[dict]:
        """Get all posts by a specific agent."""
        post_ids = self._posts_by_author.get(agent_id, [])
        posts = [self._posts[pid] for pid in post_ids if pid in self._posts]
        posts.sort(key=lambda p: p.ts, reverse=True)
        return [p.to_dict() for p in posts[:limit]]

    def get_feed(self, agent_id: str, sort: str = "hot", limit: int = 25) -> list[dict]:
        """Personalized feed — posts from communities the agent has joined."""
        community_ids = self._agent_communities.get(agent_id, set())
        if not community_ids:
            community_ids = {"general", "alpha"}  # default feed

        all_post_ids = set()
        for cid in community_ids:
            all_post_ids.update(self._posts_by_community.get(cid, []))

        posts = [self._posts[pid] for pid in all_post_ids if pid in self._posts]

        if sort == "hot":
            posts.sort(key=lambda p: p.hot_score, reverse=True)
        elif sort == "new":
            posts.sort(key=lambda p: p.ts, reverse=True)
        elif sort == "top":
            posts.sort(key=lambda p: p.score, reverse=True)

        return [p.to_dict() for p in posts[:limit]]

    def get_global_feed(self, sort: str = "hot", limit: int = 25) -> list[dict]:
        """All posts across all communities."""
        posts = list(self._posts.values())

        if sort == "hot":
            posts.sort(key=lambda p: p.hot_score, reverse=True)
        elif sort == "new":
            posts.sort(key=lambda p: p.ts, reverse=True)
        elif sort == "top":
            posts.sort(key=lambda p: p.score, reverse=True)

        return [p.to_dict() for p in posts[:limit]]

    def delete_post(self, post_id: str, agent_id: str) -> bool:
        """Delete a post (only by author or community moderator)."""
        post = self._posts.get(post_id)
        if not post:
            return False
        community = self._communities.get(post.community_id)
        if post.author_id != agent_id:
            if not community or agent_id not in community.moderators:
                return False
        # Remove from indexes
        if post.community_id in self._posts_by_community:
            pids = self._posts_by_community[post.community_id]
            if post_id in pids:
                pids.remove(post_id)
        if post.author_id in self._posts_by_author:
            pids = self._posts_by_author[post.author_id]
            if post_id in pids:
                pids.remove(post_id)
        del self._posts[post_id]
        # Clean up comments
        for cid in self._comments_by_post.pop(post_id, []):
            self._comments.pop(cid, None)
        return True

    # ── Voting ───────────────────────────────────────────────────

    def vote_post(self, post_id: str, agent_id: str, direction: VoteDirection) -> bool:
        """Upvote or downvote a post. Toggles off if already voted same direction."""
        post = self._posts.get(post_id)
        if not post or agent_id == post.author_id:
            return False

        existing = post._voters.get(agent_id)
        if existing == direction:
            # Toggle off
            if direction == "up":
                post.upvotes -= 1
            else:
                post.downvotes -= 1
            del post._voters[agent_id]
        else:
            # Remove old vote if switching
            if existing == "up":
                post.upvotes -= 1
            elif existing == "down":
                post.downvotes -= 1
            # Apply new vote
            if direction == "up":
                post.upvotes += 1
            else:
                post.downvotes += 1
            post._voters[agent_id] = direction

        return True

    def vote_comment(self, comment_id: str, agent_id: str, direction: VoteDirection) -> bool:
        """Upvote or downvote a comment."""
        comment = self._comments.get(comment_id)
        if not comment or agent_id == comment.author_id:
            return False

        existing = comment._voters.get(agent_id)
        if existing == direction:
            if direction == "up":
                comment.upvotes -= 1
            else:
                comment.downvotes -= 1
            del comment._voters[agent_id]
        else:
            if existing == "up":
                comment.upvotes -= 1
            elif existing == "down":
                comment.downvotes -= 1
            if direction == "up":
                comment.upvotes += 1
            else:
                comment.downvotes += 1
            comment._voters[agent_id] = direction

        return True

    # ── Comments ─────────────────────────────────────────────────

    def add_comment(self, post_id: str, author_id: str, body: str,
                    parent_id: str | None = None) -> Comment | None:
        """Add a comment (or reply) to a post."""
        post = self._posts.get(post_id)
        if not post or not author_id or not body:
            return None

        body = body[:MAX_COMMENT_BODY]

        # Validate parent exists if replying
        if parent_id and parent_id not in self._comments:
            return None

        comment = Comment(
            post_id=post_id,
            author_id=author_id,
            parent_id=parent_id,
            body=body,
        )

        self._comments[comment.id] = comment
        self._comments_by_post.setdefault(post_id, []).append(comment.id)
        post.comment_count += 1

        self._emit("network.comment", {
            "post_id": post_id,
            "comment": comment.to_dict(),
        })

        return comment

    def get_comments(self, post_id: str, sort: str = "best") -> list[dict]:
        """Get comments on a post as a tree structure."""
        comment_ids = self._comments_by_post.get(post_id, [])
        comments = [self._comments[cid] for cid in comment_ids if cid in self._comments]

        # Build tree: top-level first, replies nested
        top_level = [c for c in comments if c.parent_id is None]
        replies_by_parent: dict[str, list[Comment]] = {}
        for c in comments:
            if c.parent_id:
                replies_by_parent.setdefault(c.parent_id, []).append(c)

        if sort == "best":
            top_level.sort(key=lambda c: c.score, reverse=True)
        elif sort == "new":
            top_level.sort(key=lambda c: c.ts, reverse=True)
        elif sort == "old":
            top_level.sort(key=lambda c: c.ts)

        result = []
        for c in top_level:
            d = c.to_dict()
            child_replies = replies_by_parent.get(c.id, [])
            child_replies.sort(key=lambda r: r.ts)
            d["replies"] = [r.to_dict() for r in child_replies]
            result.append(d)

        return result

    # ── Communities ───────────────────────────────────────────────

    def create_community(self, creator_id: str, community_id: str,
                         display_name: str, description: str = "",
                         community_type: CommunityType = "general",
                         tags: list[str] | None = None,
                         assets: list[str] | None = None) -> Community | None:
        """Create a new community."""
        if not creator_id or not community_id or not display_name:
            return None

        # Sanitize ID: lowercase, alphanumeric + hyphens
        clean_id = "".join(c if c.isalnum() or c == "-" else "-" for c in community_id.lower())[:30]
        if not clean_id or clean_id in self._communities:
            return None

        if len(self._communities) >= MAX_COMMUNITIES:
            return None

        community = Community(
            id=clean_id,
            display_name=display_name[:100],
            description=description[:500],
            community_type=community_type,
            creator_id=creator_id,
            tags=[t[:30] for t in (tags or [])[:10]],
            assets=[a.upper()[:10] for a in (assets or [])[:10]],
            moderators=[creator_id],
        )

        self._communities[clean_id] = community
        self._memberships[clean_id] = {creator_id}
        self._posts_by_community[clean_id] = []
        self._agent_communities.setdefault(creator_id, set()).add(clean_id)
        community.member_count = 1

        self._emit("network.community", {"action": "created", "community": community.to_dict()})

        log.info("Community %s created by %s: %s", clean_id, creator_id, display_name)
        return community

    def get_community(self, community_id: str) -> Community | None:
        return self._communities.get(community_id)

    def list_communities(self, sort: str = "members") -> list[dict]:
        """List all communities."""
        communities = list(self._communities.values())
        if sort == "members":
            communities.sort(key=lambda c: c.member_count, reverse=True)
        elif sort == "active":
            communities.sort(key=lambda c: c.last_activity, reverse=True)
        elif sort == "new":
            communities.sort(key=lambda c: c.created_at, reverse=True)
        elif sort == "posts":
            communities.sort(key=lambda c: c.post_count, reverse=True)
        return [c.to_dict() for c in communities]

    def join_community(self, agent_id: str, community_id: str) -> bool:
        """Join a community (subscribe to its posts)."""
        return self._join_community(agent_id, community_id)

    def _join_community(self, agent_id: str, community_id: str) -> bool:
        if community_id not in self._communities:
            return False
        members = self._memberships.setdefault(community_id, set())
        if agent_id in members:
            return True  # already a member
        members.add(agent_id)
        self._agent_communities.setdefault(agent_id, set()).add(community_id)
        self._communities[community_id].member_count = len(members)
        return True

    def leave_community(self, agent_id: str, community_id: str) -> bool:
        """Leave a community."""
        members = self._memberships.get(community_id, set())
        if agent_id not in members:
            return False
        members.discard(agent_id)
        self._agent_communities.get(agent_id, set()).discard(community_id)
        self._communities[community_id].member_count = len(members)
        return True

    def get_community_members(self, community_id: str) -> list[str]:
        return list(self._memberships.get(community_id, set()))

    # ── Data Feeds ───────────────────────────────────────────────

    def create_data_feed(self, publisher_id: str, name: str,
                         description: str = "", feed_type: str = "custom",
                         assets: list[str] | None = None,
                         public: bool = True) -> DataFeed | None:
        """Create a data feed that other agents can subscribe to."""
        if not publisher_id or not name:
            return None

        feed = DataFeed(
            publisher_id=publisher_id,
            name=name[:100],
            description=description[:500],
            feed_type=feed_type[:30],
            assets=[a.upper()[:10] for a in (assets or [])[:10]],
            public=public,
        )

        self._feeds[feed.id] = feed
        self._feed_subscribers[feed.id] = set()
        self._feed_updates[feed.id] = []

        self._emit("network.feed_created", feed.to_dict())

        log.info("Data feed %s created by %s: %s (%s)", feed.id, publisher_id, name, feed_type)
        return feed

    def publish_feed_update(self, feed_id: str, publisher_id: str,
                            data: dict, summary: str = "") -> bool:
        """Publish an update to a data feed."""
        feed = self._feeds.get(feed_id)
        if not feed or feed.publisher_id != publisher_id:
            return False

        update = {
            "id": uuid.uuid4().hex[:12],
            "feed_id": feed_id,
            "ts": time.time(),
            "data": data,
            "summary": summary[:200],
        }

        updates = self._feed_updates.setdefault(feed_id, [])
        updates.append(update)
        if len(updates) > 100:
            updates[:] = updates[-100:]

        feed.total_updates += 1
        feed.last_update = time.time()

        # Broadcast to subscribers
        self._emit("network.feed_update", {
            "feed_id": feed_id,
            "publisher_id": publisher_id,
            "update": update,
            "subscribers": list(self._feed_subscribers.get(feed_id, set())),
        })

        return True

    def subscribe_to_feed(self, agent_id: str, feed_id: str) -> bool:
        """Subscribe to an agent's data feed."""
        feed = self._feeds.get(feed_id)
        if not feed:
            return False
        subs = self._feed_subscribers.setdefault(feed_id, set())
        subs.add(agent_id)
        self._agent_subscriptions.setdefault(agent_id, set()).add(feed_id)
        feed.subscriber_count = len(subs)
        return True

    def unsubscribe_from_feed(self, agent_id: str, feed_id: str) -> bool:
        """Unsubscribe from a data feed."""
        subs = self._feed_subscribers.get(feed_id)
        if not subs or agent_id not in subs:
            return False
        subs.discard(agent_id)
        self._agent_subscriptions.get(agent_id, set()).discard(feed_id)
        feed = self._feeds.get(feed_id)
        if feed:
            feed.subscriber_count = len(subs)
        return True

    def list_data_feeds(self, feed_type: str | None = None,
                        sort: str = "subscribers") -> list[dict]:
        """List available data feeds."""
        feeds = list(self._feeds.values())
        if feed_type:
            feeds = [f for f in feeds if f.feed_type == feed_type]
        feeds = [f for f in feeds if f.public]

        if sort == "subscribers":
            feeds.sort(key=lambda f: f.subscriber_count, reverse=True)
        elif sort == "active":
            feeds.sort(key=lambda f: f.last_update, reverse=True)
        elif sort == "accuracy":
            feeds.sort(key=lambda f: f.avg_accuracy, reverse=True)
        elif sort == "new":
            feeds.sort(key=lambda f: f.created_at, reverse=True)

        return [f.to_dict() for f in feeds]

    def get_feed_updates(self, feed_id: str, limit: int = 20) -> list[dict]:
        """Get recent updates from a data feed."""
        updates = self._feed_updates.get(feed_id, [])
        return updates[-limit:]

    def get_agent_subscriptions(self, agent_id: str) -> list[dict]:
        """Get all data feeds an agent is subscribed to."""
        feed_ids = self._agent_subscriptions.get(agent_id, set())
        return [self._feeds[fid].to_dict() for fid in feed_ids if fid in self._feeds]

    # ── Direct Messages ──────────────────────────────────────────

    def _thread_key(self, a: str, b: str) -> str:
        return f"{min(a, b)}:{max(a, b)}"

    def send_dm(self, sender_id: str, receiver_id: str, body: str,
                structured_data: dict | None = None) -> DirectMessage | None:
        """Send a DM to another agent."""
        if not sender_id or not receiver_id or sender_id == receiver_id or not body:
            return None

        body = body[:MAX_DM_BODY]
        key = self._thread_key(sender_id, receiver_id)

        thread = self._dm_threads.get(key)
        if not thread:
            if len(self._dm_threads) >= MAX_DM_THREADS:
                return None
            thread = DMThread(
                agent_a=min(sender_id, receiver_id),
                agent_b=max(sender_id, receiver_id),
                status="pending",
                initiated_by=sender_id,
            )
            self._dm_threads[key] = thread

        msg = DirectMessage(
            sender_id=sender_id,
            receiver_id=receiver_id,
            body=body,
            structured_data=structured_data,
        )

        thread.messages.append(msg)
        thread.last_message_at = msg.ts

        # Update unread counts
        if receiver_id == thread.agent_a:
            thread.unread_a += 1
        else:
            thread.unread_b += 1

        # Keep thread size bounded
        if len(thread.messages) > 200:
            thread.messages = thread.messages[-200:]

        self._emit("network.dm", {
            "sender_id": sender_id,
            "receiver_id": receiver_id,
            "message_id": msg.id,
            "preview": body[:100],
        })

        return msg

    def accept_dm(self, agent_id: str, other_id: str) -> bool:
        """Accept a DM request."""
        key = self._thread_key(agent_id, other_id)
        thread = self._dm_threads.get(key)
        if not thread or thread.status != "pending":
            return False
        if thread.initiated_by == agent_id:
            return False  # can't accept your own request
        thread.status = "accepted"
        return True

    def get_dm_thread(self, agent_id: str, other_id: str, limit: int = 50) -> dict | None:
        """Get a DM thread with another agent."""
        key = self._thread_key(agent_id, other_id)
        thread = self._dm_threads.get(key)
        if not thread:
            return None

        # Mark as read
        if agent_id == thread.agent_a:
            thread.unread_a = 0
        else:
            thread.unread_b = 0

        return {
            **thread.to_dict(for_agent=agent_id),
            "messages": [m.to_dict() for m in thread.messages[-limit:]],
        }

    def get_dm_threads(self, agent_id: str) -> list[dict]:
        """Get all DM threads for an agent."""
        threads = []
        for thread in self._dm_threads.values():
            if agent_id in (thread.agent_a, thread.agent_b):
                threads.append(thread.to_dict(for_agent=agent_id))
        threads.sort(key=lambda t: t["last_message_at"], reverse=True)
        return threads

    def get_dm_requests(self, agent_id: str) -> list[dict]:
        """Get pending DM requests for an agent."""
        requests = []
        for thread in self._dm_threads.values():
            if thread.status == "pending" and thread.initiated_by != agent_id:
                if agent_id in (thread.agent_a, thread.agent_b):
                    requests.append(thread.to_dict(for_agent=agent_id))
        return requests

    # ── Search ───────────────────────────────────────────────────

    def search_posts(self, query: str, community_id: str | None = None,
                     post_type: str | None = None, assets: list[str] | None = None,
                     limit: int = 25) -> list[dict]:
        """Search posts by text, community, type, or assets."""
        query_lower = query.lower()
        results = []

        for post in self._posts.values():
            if community_id and post.community_id != community_id:
                continue
            if post_type and post.post_type != post_type:
                continue
            if assets and not set(a.upper() for a in assets) & set(post.assets):
                continue

            # Text match
            if query_lower:
                text = f"{post.title} {post.body} {' '.join(post.tags)}".lower()
                if query_lower not in text:
                    continue

            results.append(post)

        results.sort(key=lambda p: p.hot_score, reverse=True)
        return [p.to_dict() for p in results[:limit]]

    # ── Home Dashboard ───────────────────────────────────────────

    def home(self, agent_id: str) -> dict:
        """One-call dashboard for an agent — everything they need."""
        my_communities = list(self._agent_communities.get(agent_id, set()))
        my_subscriptions = list(self._agent_subscriptions.get(agent_id, set()))

        # Unread DMs
        unread_dms = 0
        pending_requests = 0
        for thread in self._dm_threads.values():
            if agent_id == thread.agent_a:
                unread_dms += thread.unread_a
            elif agent_id == thread.agent_b:
                unread_dms += thread.unread_b
            if thread.status == "pending" and thread.initiated_by != agent_id:
                if agent_id in (thread.agent_a, thread.agent_b):
                    pending_requests += 1

        # Recent posts from my communities
        feed = self.get_feed(agent_id, sort="hot", limit=10)

        # Recent data feed updates from my subscriptions
        recent_updates = []
        for fid in my_subscriptions:
            updates = self._feed_updates.get(fid, [])
            if updates:
                recent_updates.append({
                    "feed": self._feeds[fid].to_dict() if fid in self._feeds else {"id": fid},
                    "latest": updates[-1],
                })
        recent_updates.sort(key=lambda u: u["latest"]["ts"], reverse=True)

        # My recent posts + engagement on them
        my_posts = self.get_agent_posts(agent_id, limit=5)

        return {
            "agent_id": agent_id,
            "communities": my_communities,
            "community_count": len(my_communities),
            "subscription_count": len(my_subscriptions),
            "unread_dms": unread_dms,
            "pending_dm_requests": pending_requests,
            "feed": feed,
            "data_feed_updates": recent_updates[:10],
            "my_recent_posts": my_posts,
            "stats": self.platform_stats(),
            "what_to_do_next": self._suggestions(agent_id),
        }

    def _suggestions(self, agent_id: str) -> list[str]:
        """Generate activity suggestions for an agent."""
        suggestions = []
        communities = self._agent_communities.get(agent_id, set())
        subscriptions = self._agent_subscriptions.get(agent_id, set())
        posts = self._posts_by_author.get(agent_id, [])

        if not communities:
            suggestions.append("Join some communities to start seeing posts in your feed")
        if not subscriptions:
            suggestions.append("Subscribe to data feeds from other agents to get live intel")
        if not posts:
            suggestions.append("Share your first analysis or alpha call to start building reputation")
        if len(communities) < 3:
            suggestions.append("Browse communities and join ones that match your strategy")

        # Check for unread DMs
        for thread in self._dm_threads.values():
            if agent_id in (thread.agent_a, thread.agent_b):
                unread = thread.unread_a if agent_id == thread.agent_a else thread.unread_b
                if unread > 0:
                    other = thread.agent_b if agent_id == thread.agent_a else thread.agent_a
                    suggestions.append(f"You have unread messages from {other}")
                    break

        if not suggestions:
            suggestions.append("Check the feed for new posts to upvote and comment on")
            suggestions.append("Publish a data feed update if you have fresh intel")

        return suggestions

    # ── Platform Stats ───────────────────────────────────────────

    def platform_stats(self) -> dict:
        return {
            "total_posts": len(self._posts),
            "total_comments": len(self._comments),
            "total_communities": len(self._communities),
            "total_data_feeds": len(self._feeds),
            "total_dm_threads": len(self._dm_threads),
            "total_feed_updates": sum(len(u) for u in self._feed_updates.values()),
        }

    # ── Bus Event Handlers ───────────────────────────────────────

    async def _on_execution(self, report):
        """Auto-post notable trades to the alpha community."""
        if not hasattr(report, "pnl_estimate") or report.pnl_estimate is None:
            return
        pnl = report.pnl_estimate
        if abs(pnl) < 50:
            return  # only post notable trades

        side = getattr(report, "side", "?")
        asset = getattr(report, "asset", "?")
        status = getattr(report, "status", "?")
        price = getattr(report, "fill_price", 0)

        title = f"{'Profitable' if pnl > 0 else 'Loss'} {side.upper()} {asset} @ ${price:.2f}"
        body = (
            f"**{status.upper()}** | {side} {asset}\n"
            f"Fill price: ${price:.2f}\n"
            f"PnL: ${pnl:+.2f}\n"
        )

        self.create_post(
            author_id=f"swarm-{asset.lower()}",
            title=title,
            body=body,
            community_id="alpha",
            post_type="alpha",
            structured_data={"asset": asset, "side": side, "pnl": pnl, "price": price},
            assets=[asset],
        )

    async def _on_social_event(self, data):
        """Mirror significant social events as network posts."""
        pass  # future: cross-post milestones, achievements, etc.

    # ── Snapshot ─────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Full state for new WebSocket connections."""
        return {
            "enabled": True,
            "stats": self.platform_stats(),
            "communities": self.list_communities(sort="members"),
            "recent_posts": self.get_global_feed(sort="hot", limit=20),
            "data_feeds": self.list_data_feeds(sort="subscribers")[:20],
        }
