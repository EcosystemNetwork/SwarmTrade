"""Database migration system — versioned schema changes with rollback safety.

Tracks applied migrations in a `schema_migrations` table. Each migration
is a (version, name, up_sql) tuple. Migrations run in order, skip if
already applied, and log every change.

Usage:
    db = Database(...)
    await db.connect()
    await run_migrations(db)
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("swarm.migrations")

# ── Migration Registry ─────────────────────────────────────────
# Each tuple: (version: int, name: str, up_sql: str)
# Add new migrations at the end. NEVER reorder or modify existing ones.

MIGRATIONS: list[tuple[int, str, str]] = [
    (1, "initial_schema", """
        -- Core trading tables
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
        CREATE INDEX IF NOT EXISTS idx_reports_intent_id ON reports(intent_id);
        CREATE INDEX IF NOT EXISTS idx_reports_ts ON reports(ts);
        CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status);
        CREATE INDEX IF NOT EXISTS idx_intents_intent_id ON intents(intent_id);
        CREATE INDEX IF NOT EXISTS idx_intents_ts ON intents(ts);
        CREATE INDEX IF NOT EXISTS idx_wallet_txs_ts ON wallet_transactions(ts);
        CREATE INDEX IF NOT EXISTS idx_wallet_txs_type ON wallet_transactions(tx_type);
    """),

    (2, "social_trading", """
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
            follower_id TEXT NOT NULL, leader_id TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (follower_id, leader_id)
        );
        CREATE TABLE IF NOT EXISTS referral_links (
            code TEXT PRIMARY KEY, referrer_id TEXT NOT NULL,
            tier1_pct DOUBLE PRECISION DEFAULT 10.0,
            tier2_pct DOUBLE PRECISION DEFAULT 3.0,
            total_referrals INTEGER DEFAULT 0,
            total_earnings DOUBLE PRECISION DEFAULT 0,
            active_referrals INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS social_feed (
            id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
            agent_id TEXT NOT NULL, target_id TEXT DEFAULT '',
            data JSONB DEFAULT '{}', ts DOUBLE PRECISION NOT NULL,
            likes INTEGER DEFAULT 0, created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS agent_achievements (
            agent_id TEXT NOT NULL, achievement_id TEXT NOT NULL,
            unlocked_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (agent_id, achievement_id)
        );
        CREATE INDEX IF NOT EXISTS idx_profiles_reputation ON agent_profiles(reputation_score DESC);
        CREATE INDEX IF NOT EXISTS idx_profiles_referral ON agent_profiles(referral_code);
        CREATE INDEX IF NOT EXISTS idx_copy_relations_leader ON copy_relations(leader_id);
        CREATE INDEX IF NOT EXISTS idx_copy_relations_copier ON copy_relations(copier_id);
        CREATE INDEX IF NOT EXISTS idx_copy_relations_active ON copy_relations(active);
        CREATE INDEX IF NOT EXISTS idx_follows_leader ON social_follows(leader_id);
        CREATE INDEX IF NOT EXISTS idx_follows_follower ON social_follows(follower_id);
        CREATE INDEX IF NOT EXISTS idx_referral_referrer ON referral_links(referrer_id);
        CREATE INDEX IF NOT EXISTS idx_feed_ts ON social_feed(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_feed_agent ON social_feed(agent_id);
        CREATE INDEX IF NOT EXISTS idx_feed_type ON social_feed(event_type);
        CREATE INDEX IF NOT EXISTS idx_achievements_agent ON agent_achievements(agent_id);
    """),

    (3, "swarm_network", """
        CREATE TABLE IF NOT EXISTS network_posts (
            id TEXT PRIMARY KEY, author_id TEXT NOT NULL,
            community_id TEXT NOT NULL DEFAULT 'general',
            title TEXT NOT NULL, body TEXT DEFAULT '',
            post_type TEXT DEFAULT 'analysis',
            structured_data JSONB DEFAULT '{}',
            tags JSONB DEFAULT '[]', assets JSONB DEFAULT '[]',
            upvotes INTEGER DEFAULT 0, downvotes INTEGER DEFAULT 0,
            comment_count INTEGER DEFAULT 0, view_count INTEGER DEFAULT 0,
            verified_outcome TEXT, outcome_pnl DOUBLE PRECISION,
            ts DOUBLE PRECISION NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS network_comments (
            id TEXT PRIMARY KEY, post_id TEXT NOT NULL,
            author_id TEXT NOT NULL, parent_id TEXT,
            body TEXT NOT NULL, upvotes INTEGER DEFAULT 0,
            downvotes INTEGER DEFAULT 0, ts DOUBLE PRECISION NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS network_communities (
            id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
            description TEXT DEFAULT '', community_type TEXT DEFAULT 'general',
            creator_id TEXT NOT NULL, tags JSONB DEFAULT '[]',
            assets JSONB DEFAULT '[]', member_count INTEGER DEFAULT 0,
            post_count INTEGER DEFAULT 0, moderators JSONB DEFAULT '[]',
            pinned_posts JSONB DEFAULT '[]',
            last_activity DOUBLE PRECISION DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS network_memberships (
            agent_id TEXT NOT NULL, community_id TEXT NOT NULL,
            joined_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (agent_id, community_id)
        );
        CREATE TABLE IF NOT EXISTS network_data_feeds (
            id TEXT PRIMARY KEY, publisher_id TEXT NOT NULL,
            name TEXT NOT NULL, description TEXT DEFAULT '',
            feed_type TEXT DEFAULT 'custom', assets JSONB DEFAULT '[]',
            subscriber_count INTEGER DEFAULT 0, total_updates INTEGER DEFAULT 0,
            last_update DOUBLE PRECISION DEFAULT 0,
            avg_accuracy DOUBLE PRECISION DEFAULT 0,
            public BOOLEAN DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS network_feed_subscriptions (
            agent_id TEXT NOT NULL, feed_id TEXT NOT NULL,
            subscribed_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (agent_id, feed_id)
        );
        CREATE TABLE IF NOT EXISTS network_dm_threads (
            thread_id TEXT PRIMARY KEY, agent_a TEXT NOT NULL,
            agent_b TEXT NOT NULL, status TEXT DEFAULT 'pending',
            initiated_by TEXT NOT NULL,
            last_message_at DOUBLE PRECISION DEFAULT 0,
            unread_a INTEGER DEFAULT 0, unread_b INTEGER DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS network_dms (
            id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
            sender_id TEXT NOT NULL, receiver_id TEXT NOT NULL,
            body TEXT NOT NULL, structured_data JSONB,
            read BOOLEAN DEFAULT FALSE, ts DOUBLE PRECISION NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE TABLE IF NOT EXISTS network_votes (
            agent_id TEXT NOT NULL, target_id TEXT NOT NULL,
            target_type TEXT NOT NULL, direction TEXT NOT NULL,
            voted_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (agent_id, target_id)
        );
        CREATE INDEX IF NOT EXISTS idx_net_posts_community ON network_posts(community_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_net_posts_author ON network_posts(author_id, ts DESC);
        CREATE INDEX IF NOT EXISTS idx_net_posts_type ON network_posts(post_type);
        CREATE INDEX IF NOT EXISTS idx_net_posts_ts ON network_posts(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_net_comments_post ON network_comments(post_id, ts);
        CREATE INDEX IF NOT EXISTS idx_net_comments_author ON network_comments(author_id);
        CREATE INDEX IF NOT EXISTS idx_net_memberships_community ON network_memberships(community_id);
        CREATE INDEX IF NOT EXISTS idx_net_memberships_agent ON network_memberships(agent_id);
        CREATE INDEX IF NOT EXISTS idx_net_feeds_publisher ON network_data_feeds(publisher_id);
        CREATE INDEX IF NOT EXISTS idx_net_feeds_type ON network_data_feeds(feed_type);
        CREATE INDEX IF NOT EXISTS idx_net_feed_subs_feed ON network_feed_subscriptions(feed_id);
        CREATE INDEX IF NOT EXISTS idx_net_feed_subs_agent ON network_feed_subscriptions(agent_id);
        CREATE INDEX IF NOT EXISTS idx_net_dms_thread ON network_dms(thread_id, ts);
        CREATE INDEX IF NOT EXISTS idx_net_dm_threads_agents ON network_dm_threads(agent_a);
        CREATE INDEX IF NOT EXISTS idx_net_votes_target ON network_votes(target_id);
    """),

    (4, "metrics_table", """
        CREATE TABLE IF NOT EXISTS metrics_snapshots (
            id          BIGSERIAL PRIMARY KEY,
            ts          DOUBLE PRECISION NOT NULL,
            metric_type TEXT NOT NULL,
            data        JSONB NOT NULL,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics_snapshots(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_metrics_type ON metrics_snapshots(metric_type);
    """),
]

# ── Migration Version Table ──────────────────────────────────────

MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    applied_at  TIMESTAMPTZ DEFAULT NOW(),
    duration_ms DOUBLE PRECISION DEFAULT 0
);
"""


# ── Runner ───────────────────────────────────────────────────────

async def run_migrations(db) -> int:
    """Run all pending migrations. Returns count of newly applied migrations.

    Safe to call on every startup — skips already-applied versions.
    Migrations use Postgres DDL syntax. For SQLite, the existing
    SQLITE_SCHEMA in database.py handles schema creation (SQLite is
    only used for local dev/backtest, not production).
    """
    if not db._connected:
        raise RuntimeError("Database not connected — call db.connect() first")

    if not db.is_postgres:
        log.info("SQLite backend — migrations skipped (schema managed by SQLITE_SCHEMA)")
        return 0

    # Create migration tracking table
    await db.execute(MIGRATION_TABLE_SQL)

    # Get already-applied versions
    rows = await db.fetch("SELECT version FROM schema_migrations ORDER BY version")
    applied = {r["version"] for r in rows}

    applied_count = 0
    for version, name, up_sql in MIGRATIONS:
        if version in applied:
            continue

        log.info("Applying migration %d: %s ...", version, name)
        start = time.monotonic()

        try:
            await db.execute(up_sql)
            duration_ms = (time.monotonic() - start) * 1000

            await db.execute(
                "INSERT INTO schema_migrations (version, name, duration_ms) "
                "VALUES ($1, $2, $3)",
                version, name, duration_ms,
            )

            applied_count += 1
            log.info("  Migration %d applied in %.1fms", version, duration_ms)

        except Exception as e:
            log.error("Migration %d (%s) FAILED: %s", version, name, e)
            raise RuntimeError(f"Migration {version} ({name}) failed: {e}") from e

    if applied_count == 0:
        log.info("Database schema up to date (version %d)",
                 max((v for v, _, _ in MIGRATIONS), default=0))
    else:
        log.info("Applied %d migration(s), now at version %d",
                 applied_count, max(v for v, _, _ in MIGRATIONS))

    return applied_count


async def get_migration_status(db) -> dict:
    """Get current migration state for health checks."""
    if not db.is_postgres:
        return {"current_version": 0, "latest_version": 0,
                "up_to_date": True, "backend": "sqlite", "note": "SQLite uses inline schema"}
    try:
        rows = await db.fetch(
            "SELECT version, name, applied_at, duration_ms "
            "FROM schema_migrations ORDER BY version"
        )
        latest = max((v for v, _, _ in MIGRATIONS), default=0)
        current = max((r["version"] for r in rows), default=0)
        return {
            "current_version": current,
            "latest_version": latest,
            "up_to_date": current >= latest,
            "pending": latest - current,
            "applied": [dict(r) for r in rows],
        }
    except Exception:
        return {"current_version": 0, "latest_version": 0, "up_to_date": False, "error": "no migration table"}
