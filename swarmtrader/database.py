"""Async Postgres (Neon) database layer with SQLite fallback.

Provides a unified Database interface that:
- Connects to Neon Postgres via DATABASE_URL when available
- Falls back to local SQLite for offline/backtest use
- Manages connection pooling for Postgres
- Initializes schema (tables + indexes) on startup
- Exposes async execute/fetch methods matching both drivers

Usage:
    db = Database(database_url=os.getenv("DATABASE_URL"))
    await db.connect()
    rows = await db.fetch("SELECT * FROM reports WHERE status=$1", "filled")
    await db.close()
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("swarm.db")

# Retry config for transient DB errors (Neon cold starts, network blips)
_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.5  # seconds, doubles each retry


async def _retry_pg(fn, *args, **kwargs):
    """Execute an async Postgres operation with exponential backoff retry."""
    last_exc = None
    for attempt in range(_MAX_RETRIES):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            err_str = str(e).lower()
            # Only retry on transient/connection errors
            transient = any(k in err_str for k in (
                "connection", "timeout", "closed", "pool", "broken pipe",
                "server closed", "ssl", "operational",
            ))
            if not transient or attempt == _MAX_RETRIES - 1:
                raise
            delay = _RETRY_BASE_DELAY * (2 ** attempt)
            log.warning("DB transient error (attempt %d/%d, retry in %.1fs): %s",
                        attempt + 1, _MAX_RETRIES, delay, e)
            await asyncio.sleep(delay)
    raise last_exc  # unreachable, but satisfies type checker

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id          BIGSERIAL PRIMARY KEY,
    ts          DOUBLE PRECISION NOT NULL,
    intent_id   TEXT NOT NULL,
    status      TEXT NOT NULL,
    tx          TEXT,
    fill_price  DOUBLE PRECISION,
    slippage    DOUBLE PRECISION,
    pnl         DOUBLE PRECISION,
    fee         DOUBLE PRECISION,
    side        TEXT,
    quantity    DOUBLE PRECISION,
    asset       TEXT,
    note        TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS intents (
    id          BIGSERIAL PRIMARY KEY,
    ts          DOUBLE PRECISION NOT NULL,
    intent_id   TEXT NOT NULL,
    asset_in    TEXT NOT NULL,
    asset_out   TEXT NOT NULL,
    amount_in   DOUBLE PRECISION NOT NULL,
    supporting  JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallet_state (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS wallet_transactions (
    id          BIGSERIAL PRIMARY KEY,
    ts          DOUBLE PRECISION NOT NULL,
    tx_type     TEXT NOT NULL,
    amount      DOUBLE PRECISION NOT NULL,
    note        TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Social trading layer
CREATE TABLE IF NOT EXISTS agent_profiles (
    agent_id            TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    bio                 TEXT DEFAULT '',
    avatar_seed         TEXT DEFAULT '',
    visibility          TEXT DEFAULT 'public',
    strategy_tags       JSONB DEFAULT '[]',
    strategy_description TEXT DEFAULT '',
    referral_code       TEXT UNIQUE,
    verified            BOOLEAN DEFAULT FALSE,
    total_trades        INTEGER DEFAULT 0,
    winning_trades      INTEGER DEFAULT 0,
    total_pnl           DOUBLE PRECISION DEFAULT 0,
    max_drawdown        DOUBLE PRECISION DEFAULT 0,
    sharpe_ratio        DOUBLE PRECISION DEFAULT 0,
    reputation_score    DOUBLE PRECISION DEFAULT 0,
    best_trade_pnl      DOUBLE PRECISION DEFAULT 0,
    worst_trade_pnl     DOUBLE PRECISION DEFAULT 0,
    followers_count     INTEGER DEFAULT 0,
    copiers_count       INTEGER DEFAULT 0,
    total_copy_capital  DOUBLE PRECISION DEFAULT 0,
    total_earned_fees   DOUBLE PRECISION DEFAULT 0,
    total_referral_earnings DOUBLE PRECISION DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS copy_relations (
    id                  TEXT PRIMARY KEY,
    copier_id           TEXT NOT NULL,
    leader_id           TEXT NOT NULL,
    active              BOOLEAN DEFAULT TRUE,
    allocation          DOUBLE PRECISION DEFAULT 1000,
    max_trade_size      DOUBLE PRECISION DEFAULT 500,
    size_multiplier     DOUBLE PRECISION DEFAULT 1.0,
    copy_longs          BOOLEAN DEFAULT TRUE,
    copy_shorts         BOOLEAN DEFAULT TRUE,
    max_daily_loss      DOUBLE PRECISION DEFAULT 100,
    min_confidence      DOUBLE PRECISION DEFAULT 0.3,
    management_fee_pct  DOUBLE PRECISION DEFAULT 2.0,
    performance_fee_pct DOUBLE PRECISION DEFAULT 20.0,
    high_water_mark     DOUBLE PRECISION DEFAULT 0,
    total_copied_trades INTEGER DEFAULT 0,
    total_pnl           DOUBLE PRECISION DEFAULT 0,
    total_fees_paid     DOUBLE PRECISION DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS social_follows (
    follower_id         TEXT NOT NULL,
    leader_id           TEXT NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (follower_id, leader_id)
);

CREATE TABLE IF NOT EXISTS referral_links (
    code                TEXT PRIMARY KEY,
    referrer_id         TEXT NOT NULL,
    tier1_pct           DOUBLE PRECISION DEFAULT 10.0,
    tier2_pct           DOUBLE PRECISION DEFAULT 3.0,
    total_referrals     INTEGER DEFAULT 0,
    total_earnings      DOUBLE PRECISION DEFAULT 0,
    active_referrals    INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS social_feed (
    id                  TEXT PRIMARY KEY,
    event_type          TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    target_id           TEXT DEFAULT '',
    data                JSONB DEFAULT '{}',
    ts                  DOUBLE PRECISION NOT NULL,
    likes               INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_achievements (
    agent_id            TEXT NOT NULL,
    achievement_id      TEXT NOT NULL,
    unlocked_at         TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (agent_id, achievement_id)
);

-- SwarmNetwork: agent social network for intelligence sharing
CREATE TABLE IF NOT EXISTS network_posts (
    id                  TEXT PRIMARY KEY,
    author_id           TEXT NOT NULL,
    community_id        TEXT NOT NULL DEFAULT 'general',
    title               TEXT NOT NULL,
    body                TEXT DEFAULT '',
    post_type           TEXT DEFAULT 'analysis',
    structured_data     JSONB DEFAULT '{}',
    tags                JSONB DEFAULT '[]',
    assets              JSONB DEFAULT '[]',
    upvotes             INTEGER DEFAULT 0,
    downvotes           INTEGER DEFAULT 0,
    comment_count       INTEGER DEFAULT 0,
    view_count          INTEGER DEFAULT 0,
    verified_outcome    TEXT,
    outcome_pnl         DOUBLE PRECISION,
    ts                  DOUBLE PRECISION NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS network_comments (
    id                  TEXT PRIMARY KEY,
    post_id             TEXT NOT NULL,
    author_id           TEXT NOT NULL,
    parent_id           TEXT,
    body                TEXT NOT NULL,
    upvotes             INTEGER DEFAULT 0,
    downvotes           INTEGER DEFAULT 0,
    ts                  DOUBLE PRECISION NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS network_communities (
    id                  TEXT PRIMARY KEY,
    display_name        TEXT NOT NULL,
    description         TEXT DEFAULT '',
    community_type      TEXT DEFAULT 'general',
    creator_id          TEXT NOT NULL,
    tags                JSONB DEFAULT '[]',
    assets              JSONB DEFAULT '[]',
    member_count        INTEGER DEFAULT 0,
    post_count          INTEGER DEFAULT 0,
    moderators          JSONB DEFAULT '[]',
    pinned_posts        JSONB DEFAULT '[]',
    last_activity       DOUBLE PRECISION DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS network_memberships (
    agent_id            TEXT NOT NULL,
    community_id        TEXT NOT NULL,
    joined_at           TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (agent_id, community_id)
);

CREATE TABLE IF NOT EXISTS network_data_feeds (
    id                  TEXT PRIMARY KEY,
    publisher_id        TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT DEFAULT '',
    feed_type           TEXT DEFAULT 'custom',
    assets              JSONB DEFAULT '[]',
    subscriber_count    INTEGER DEFAULT 0,
    total_updates       INTEGER DEFAULT 0,
    last_update         DOUBLE PRECISION DEFAULT 0,
    avg_accuracy        DOUBLE PRECISION DEFAULT 0,
    public              BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS network_feed_subscriptions (
    agent_id            TEXT NOT NULL,
    feed_id             TEXT NOT NULL,
    subscribed_at       TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (agent_id, feed_id)
);

CREATE TABLE IF NOT EXISTS network_dm_threads (
    thread_id           TEXT PRIMARY KEY,
    agent_a             TEXT NOT NULL,
    agent_b             TEXT NOT NULL,
    status              TEXT DEFAULT 'pending',
    initiated_by        TEXT NOT NULL,
    last_message_at     DOUBLE PRECISION DEFAULT 0,
    unread_a            INTEGER DEFAULT 0,
    unread_b            INTEGER DEFAULT 0,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS network_dms (
    id                  TEXT PRIMARY KEY,
    thread_id           TEXT NOT NULL,
    sender_id           TEXT NOT NULL,
    receiver_id         TEXT NOT NULL,
    body                TEXT NOT NULL,
    structured_data     JSONB,
    read                BOOLEAN DEFAULT FALSE,
    ts                  DOUBLE PRECISION NOT NULL,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS network_votes (
    agent_id            TEXT NOT NULL,
    target_id           TEXT NOT NULL,
    target_type         TEXT NOT NULL,
    direction           TEXT NOT NULL,
    voted_at            TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (agent_id, target_id)
);
"""

POSTGRES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_reports_intent_id ON reports(intent_id)",
    "CREATE INDEX IF NOT EXISTS idx_reports_ts ON reports(ts)",
    "CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status)",
    "CREATE INDEX IF NOT EXISTS idx_intents_intent_id ON intents(intent_id)",
    "CREATE INDEX IF NOT EXISTS idx_intents_ts ON intents(ts)",
    "CREATE INDEX IF NOT EXISTS idx_wallet_txs_ts ON wallet_transactions(ts)",
    "CREATE INDEX IF NOT EXISTS idx_wallet_txs_type ON wallet_transactions(tx_type)",
    # Social trading indexes
    "CREATE INDEX IF NOT EXISTS idx_profiles_reputation ON agent_profiles(reputation_score DESC)",
    "CREATE INDEX IF NOT EXISTS idx_profiles_referral ON agent_profiles(referral_code)",
    "CREATE INDEX IF NOT EXISTS idx_copy_relations_leader ON copy_relations(leader_id)",
    "CREATE INDEX IF NOT EXISTS idx_copy_relations_copier ON copy_relations(copier_id)",
    "CREATE INDEX IF NOT EXISTS idx_copy_relations_active ON copy_relations(active)",
    "CREATE INDEX IF NOT EXISTS idx_follows_leader ON social_follows(leader_id)",
    "CREATE INDEX IF NOT EXISTS idx_follows_follower ON social_follows(follower_id)",
    "CREATE INDEX IF NOT EXISTS idx_referral_referrer ON referral_links(referrer_id)",
    "CREATE INDEX IF NOT EXISTS idx_feed_ts ON social_feed(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_feed_agent ON social_feed(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_feed_type ON social_feed(event_type)",
    "CREATE INDEX IF NOT EXISTS idx_achievements_agent ON agent_achievements(agent_id)",
    # SwarmNetwork indexes
    "CREATE INDEX IF NOT EXISTS idx_net_posts_community ON network_posts(community_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_net_posts_author ON network_posts(author_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_net_posts_type ON network_posts(post_type)",
    "CREATE INDEX IF NOT EXISTS idx_net_posts_ts ON network_posts(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_net_comments_post ON network_comments(post_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_net_comments_author ON network_comments(author_id)",
    "CREATE INDEX IF NOT EXISTS idx_net_memberships_community ON network_memberships(community_id)",
    "CREATE INDEX IF NOT EXISTS idx_net_memberships_agent ON network_memberships(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_net_feeds_publisher ON network_data_feeds(publisher_id)",
    "CREATE INDEX IF NOT EXISTS idx_net_feeds_type ON network_data_feeds(feed_type)",
    "CREATE INDEX IF NOT EXISTS idx_net_feed_subs_feed ON network_feed_subscriptions(feed_id)",
    "CREATE INDEX IF NOT EXISTS idx_net_feed_subs_agent ON network_feed_subscriptions(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_net_dms_thread ON network_dms(thread_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_net_dm_threads_agents ON network_dm_threads(agent_a)",
    "CREATE INDEX IF NOT EXISTS idx_net_votes_target ON network_votes(target_id)",
]

SQLITE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS reports(
        ts REAL, intent_id TEXT, status TEXT, tx TEXT,
        fill_price REAL, slippage REAL, pnl REAL, fee REAL,
        side TEXT, quantity REAL, asset TEXT, note TEXT)""",
    """CREATE TABLE IF NOT EXISTS intents(
        ts REAL, id TEXT, asset_in TEXT, asset_out TEXT,
        amount_in REAL, supporting TEXT)""",
    """CREATE TABLE IF NOT EXISTS wallet_state(
        key TEXT PRIMARY KEY, value TEXT)""",
    """CREATE TABLE IF NOT EXISTS wallet_transactions(
        ts REAL, tx_type TEXT, amount REAL, note TEXT)""",
    # Social trading tables
    """CREATE TABLE IF NOT EXISTS agent_profiles(
        agent_id TEXT PRIMARY KEY, display_name TEXT, bio TEXT DEFAULT '',
        avatar_seed TEXT DEFAULT '', visibility TEXT DEFAULT 'public',
        strategy_tags TEXT DEFAULT '[]', strategy_description TEXT DEFAULT '',
        referral_code TEXT UNIQUE, verified INTEGER DEFAULT 0,
        total_trades INTEGER DEFAULT 0, winning_trades INTEGER DEFAULT 0,
        total_pnl REAL DEFAULT 0, max_drawdown REAL DEFAULT 0,
        sharpe_ratio REAL DEFAULT 0, reputation_score REAL DEFAULT 0,
        best_trade_pnl REAL DEFAULT 0, worst_trade_pnl REAL DEFAULT 0,
        followers_count INTEGER DEFAULT 0, copiers_count INTEGER DEFAULT 0,
        total_copy_capital REAL DEFAULT 0, total_earned_fees REAL DEFAULT 0,
        total_referral_earnings REAL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS copy_relations(
        id TEXT PRIMARY KEY, copier_id TEXT, leader_id TEXT,
        active INTEGER DEFAULT 1, allocation REAL DEFAULT 1000,
        max_trade_size REAL DEFAULT 500, size_multiplier REAL DEFAULT 1.0,
        copy_longs INTEGER DEFAULT 1, copy_shorts INTEGER DEFAULT 1,
        max_daily_loss REAL DEFAULT 100, min_confidence REAL DEFAULT 0.3,
        management_fee_pct REAL DEFAULT 2.0, performance_fee_pct REAL DEFAULT 20.0,
        high_water_mark REAL DEFAULT 0, total_copied_trades INTEGER DEFAULT 0,
        total_pnl REAL DEFAULT 0, total_fees_paid REAL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS social_follows(
        follower_id TEXT, leader_id TEXT,
        PRIMARY KEY (follower_id, leader_id))""",
    """CREATE TABLE IF NOT EXISTS referral_links(
        code TEXT PRIMARY KEY, referrer_id TEXT,
        tier1_pct REAL DEFAULT 10.0, tier2_pct REAL DEFAULT 3.0,
        total_referrals INTEGER DEFAULT 0, total_earnings REAL DEFAULT 0,
        active_referrals INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS social_feed(
        id TEXT PRIMARY KEY, event_type TEXT, agent_id TEXT,
        target_id TEXT DEFAULT '', data TEXT DEFAULT '{}',
        ts REAL, likes INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS agent_achievements(
        agent_id TEXT, achievement_id TEXT,
        PRIMARY KEY (agent_id, achievement_id))""",
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

class Database:
    """Unified async database interface — Neon Postgres or local SQLite.

    When ``database_url`` is provided (typically ``DATABASE_URL`` env var from
    Neon/Vercel), connects to Postgres with async connection pooling via
    ``psycopg`` (psycopg 3).  Otherwise falls back to synchronous SQLite
    wrapped in ``asyncio.to_thread`` for compatibility.
    """

    def __init__(
        self,
        database_url: str | None = None,
        sqlite_path: Path | str | None = None,
        min_pool: int = 2,
        max_pool: int = 10,
    ):
        self._database_url = database_url
        self._sqlite_path = Path(sqlite_path) if sqlite_path else None
        self._min_pool = min_pool
        self._max_pool = max_pool

        # Backends (one will be populated)
        self._pool = None           # psycopg_pool.AsyncConnectionPool
        self._sqlite_conn = None    # sqlite3.Connection
        self.is_postgres = bool(database_url)
        self._connected = False

    # ── Lifecycle ──────────────────────────────────────────────────

    async def connect(self):
        """Open the connection pool (Postgres) or file handle (SQLite)."""
        if self._connected:
            return

        if self.is_postgres:
            await self._connect_postgres()
        else:
            self._connect_sqlite()

        self._connected = True
        backend = "Postgres (Neon)" if self.is_postgres else f"SQLite ({self._sqlite_path})"
        log.info("Database connected: %s", backend)

    async def check_health(self) -> dict:
        """Run a lightweight health check. Returns {"ok": bool, "backend": str, "latency_ms": float}."""
        import time as _t
        if not self._connected:
            return {"ok": False, "backend": "disconnected", "latency_ms": 0}
        start = _t.monotonic()
        try:
            if self.is_postgres:
                await self.fetchval("SELECT 1")
            else:
                await self.fetchval("SELECT 1")
            elapsed = (_t.monotonic() - start) * 1000
            backend = "postgres" if self.is_postgres else "sqlite"
            return {"ok": True, "backend": backend, "latency_ms": round(elapsed, 1)}
        except Exception as e:
            elapsed = (_t.monotonic() - start) * 1000
            return {"ok": False, "backend": "error", "latency_ms": round(elapsed, 1), "error": str(e)}

    async def close(self):
        """Drain the pool / close the file handle."""
        if not self._connected:
            return
        if self._pool:
            await self._pool.close()
            self._pool = None
        if self._sqlite_conn:
            self._sqlite_conn.close()
            self._sqlite_conn = None
        self._connected = False
        log.info("Database closed")

    # ── Query interface ────────────────────────────────────────────

    async def execute(self, query: str, *args) -> None:
        """Execute a write query (INSERT, UPDATE, DELETE)."""
        if self.is_postgres:
            await self._pg_execute(query, args)
        else:
            await self._sqlite_execute(query, args)

    async def fetch(self, query: str, *args) -> list[dict]:
        """Execute a read query, return list of dicts."""
        if self.is_postgres:
            return await self._pg_fetch(query, args)
        else:
            return await self._sqlite_fetch(query, args)

    async def fetchone(self, query: str, *args) -> dict | None:
        """Execute a read query, return first row as dict or None."""
        rows = await self.fetch(query, *args)
        return rows[0] if rows else None

    async def fetchval(self, query: str, *args):
        """Execute a read query, return the first column of the first row."""
        row = await self.fetchone(query, *args)
        if row is None:
            return None
        return next(iter(row.values()))

    # ── Postgres implementation ────────────────────────────────────

    async def _connect_postgres(self):
        try:
            from psycopg_pool import AsyncConnectionPool
            import psycopg
        except ImportError:
            raise ImportError(
                "psycopg[binary] and psycopg-pool are required for Postgres. "
                "Install with: pip install 'psycopg[binary]' psycopg-pool"
            )

        self._pool = AsyncConnectionPool(
            conninfo=self._database_url,
            min_size=self._min_pool,
            max_size=self._max_pool,
            kwargs={"autocommit": False},
            open=False,  # open manually with timeout
        )
        await self._pool.open()
        try:
            await asyncio.wait_for(self._pool.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            log.error("Database connection pool timed out after 30s")
            raise ConnectionError("Database connection pool failed to initialize within 30s")

        # Initialize schema
        async with self._pool.connection() as conn:
            await conn.execute(POSTGRES_SCHEMA)
            for idx_sql in POSTGRES_INDEXES:
                await conn.execute(idx_sql)
            await conn.commit()

        log.info("Postgres schema initialized (%d tables, %d indexes)",
                 4, len(POSTGRES_INDEXES))

    async def _pg_execute(self, query: str, args: tuple):
        async def _do():
            async with self._pool.connection() as conn:
                await conn.execute(query, args if args else None)
                await conn.commit()
        await _retry_pg(_do)

    async def _pg_fetch(self, query: str, args: tuple) -> list[dict]:
        async def _do():
            async with self._pool.connection() as conn:
                cursor = await conn.execute(query, args if args else None)
                columns = [desc.name for desc in cursor.description or []]
                rows = await cursor.fetchall()
                return [dict(zip(columns, row)) for row in rows]
        return await _retry_pg(_do)

    # ── SQLite implementation ──────────────────────────────────────

    def _connect_sqlite(self):
        import asyncio, concurrent.futures
        if not self._sqlite_path:
            self._sqlite_path = Path("swarm.db")
        self._sqlite_conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
        self._sqlite_conn.execute("PRAGMA journal_mode=WAL")  # safer concurrent access
        self._sqlite_conn.execute("PRAGMA busy_timeout=5000")  # wait up to 5s on lock
        self._sqlite_conn.row_factory = sqlite3.Row
        for sql in SQLITE_SCHEMA:
            self._sqlite_conn.execute(sql)
        self._sqlite_conn.commit()
        # Single-thread executor to serialize all SQLite operations
        self._sqlite_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="sqlite"
        )
        log.info("SQLite schema initialized (WAL mode): %s", self._sqlite_path)

    async def _sqlite_execute(self, query: str, args: tuple):
        import asyncio
        q, a = self._adapt_query(query, args)
        loop = asyncio.get_running_loop()
        def _run():
            self._sqlite_conn.execute(q, a)
            self._sqlite_conn.commit()
        await loop.run_in_executor(self._sqlite_executor, _run)

    async def _sqlite_fetch(self, query: str, args: tuple) -> list[dict]:
        import asyncio
        q, a = self._adapt_query(query, args)
        loop = asyncio.get_running_loop()
        def _run():
            rows = self._sqlite_conn.execute(q, a).fetchall()
            return [dict(r) for r in rows]
        return await loop.run_in_executor(self._sqlite_executor, _run)

    def _adapt_query(self, query: str, args: tuple) -> tuple[str, tuple]:
        """Convert Postgres $1, $2 placeholders to SQLite ? placeholders.

        Also handles JSONB casts and ON CONFLICT syntax differences.
        """
        import re
        # $N -> ? (positional)
        adapted = re.sub(r'\$\d+', '?', query)
        # ::jsonb or ::text casts -> remove
        adapted = re.sub(r'::\w+', '', adapted)
        # INSERT ... ON CONFLICT ... DO UPDATE SET -> INSERT OR REPLACE
        # (simplified — only handles the wallet_state upsert pattern)
        if "ON CONFLICT" in adapted:
            adapted = re.sub(
                r'ON CONFLICT\s*\([^)]+\)\s*DO UPDATE SET\s+.*',
                '',
                adapted,
                flags=re.DOTALL,
            )
            adapted = adapted.replace("INSERT INTO", "INSERT OR REPLACE INTO")
        return adapted, args

    # ── Convenience ────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._connected

    def raw_sqlite_conn(self) -> sqlite3.Connection | None:
        """Escape hatch: direct SQLite connection for legacy code paths.
        Returns None if using Postgres."""
        return self._sqlite_conn
