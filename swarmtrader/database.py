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

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger("swarm.db")

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
"""

POSTGRES_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_reports_intent_id ON reports(intent_id)",
    "CREATE INDEX IF NOT EXISTS idx_reports_ts ON reports(ts)",
    "CREATE INDEX IF NOT EXISTS idx_reports_status ON reports(status)",
    "CREATE INDEX IF NOT EXISTS idx_intents_intent_id ON intents(intent_id)",
    "CREATE INDEX IF NOT EXISTS idx_intents_ts ON intents(ts)",
    "CREATE INDEX IF NOT EXISTS idx_wallet_txs_ts ON wallet_transactions(ts)",
    "CREATE INDEX IF NOT EXISTS idx_wallet_txs_type ON wallet_transactions(tx_type)",
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
        )
        await self._pool.open()
        await self._pool.wait()

        # Initialize schema
        async with self._pool.connection() as conn:
            await conn.execute(POSTGRES_SCHEMA)
            for idx_sql in POSTGRES_INDEXES:
                await conn.execute(idx_sql)
            await conn.commit()

        log.info("Postgres schema initialized (%d tables, %d indexes)",
                 4, len(POSTGRES_INDEXES))

    async def _pg_execute(self, query: str, args: tuple):
        async with self._pool.connection() as conn:
            await conn.execute(query, args if args else None)
            await conn.commit()

    async def _pg_fetch(self, query: str, args: tuple) -> list[dict]:
        async with self._pool.connection() as conn:
            cursor = await conn.execute(query, args if args else None)
            columns = [desc.name for desc in cursor.description or []]
            rows = await cursor.fetchall()
            return [dict(zip(columns, row)) for row in rows]

    # ── SQLite implementation ──────────────────────────────────────

    def _connect_sqlite(self):
        import asyncio
        if not self._sqlite_path:
            self._sqlite_path = Path("swarm.db")
        self._sqlite_conn = sqlite3.connect(self._sqlite_path, check_same_thread=False)
        self._sqlite_conn.row_factory = sqlite3.Row
        for sql in SQLITE_SCHEMA:
            self._sqlite_conn.execute(sql)
        self._sqlite_conn.commit()
        log.info("SQLite schema initialized: %s", self._sqlite_path)

    async def _sqlite_execute(self, query: str, args: tuple):
        import asyncio
        q, a = self._adapt_query(query, args)
        def _run():
            self._sqlite_conn.execute(q, a)
            self._sqlite_conn.commit()
        await asyncio.to_thread(_run)

    async def _sqlite_fetch(self, query: str, args: tuple) -> list[dict]:
        import asyncio
        q, a = self._adapt_query(query, args)
        def _run():
            rows = self._sqlite_conn.execute(q, a).fetchall()
            return [dict(r) for r in rows]
        return await asyncio.to_thread(_run)

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
