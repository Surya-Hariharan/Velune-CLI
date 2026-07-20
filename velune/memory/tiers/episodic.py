"""Episodic Memory Tier (Tier 2).

SQLite-backed store for structured historical reads of execution steps,
intermediate tool outputs, and complete conversation traces.

All I/O is async and routed through :class:`~velune.memory.storage.sqlite_pool.SQLiteConnectionPool`
so concurrent coroutines never cause WAL write contention.

Phase 2a also introduces :class:`EpisodicMemory` — a higher-level session/turn
store with schema migrations, content search, and CognitiveBus integration.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from velune.memory.storage.sqlite_pool import SQLiteConnectionPool

logger = logging.getLogger("velune.memory.tiers.episodic")

_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS conversation_turns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        timestamp REAL NOT NULL,
        metadata TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS execution_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT NOT NULL,
        step_name TEXT NOT NULL,
        status TEXT NOT NULL,
        payload TEXT NOT NULL,
        timestamp REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_turns_session ON conversation_turns(session_id);
    CREATE INDEX IF NOT EXISTS idx_steps_session ON execution_steps(session_id);
"""


class EpisodicTurn(BaseModel):
    """An episodic conversation turn stored in SQLite."""

    id: int | None = None
    session_id: str
    role: str
    content: str
    timestamp: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class EpisodicStep(BaseModel):
    """An episodic execution step stored in SQLite."""

    id: int | None = None
    session_id: str
    step_name: str
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: float


class EpisodicMemoryTier:
    """Tier 2: Persisted SQLite storage for local episodic trace retrieval.

    Requires a :class:`~velune.memory.storage.sqlite_pool.SQLiteConnectionPool`
    that has already been started.  Call ``await tier.initialize()`` before
    any read or write operations; the :class:`~velune.kernel.bootstrap.RuntimeBootstrapper`
    does this automatically when ``lifecycle_key`` is set.
    """

    def __init__(self, pool: SQLiteConnectionPool) -> None:
        self._pool = pool

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Create tables and indexes if they do not yet exist."""
        await self._init_db()

    async def _init_db(self) -> None:
        async with self._pool.write() as conn:
            for stmt in _SCHEMA_SQL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await conn.execute(stmt)
        logger.debug("EpisodicMemoryTier schema initialised.")

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def add_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Persist a conversation turn."""
        meta_str = json.dumps(metadata or {})
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    "INSERT INTO conversation_turns"
                    " (session_id, role, content, timestamp, metadata)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (session_id, role, content, time.time(), meta_str),
                )
        except Exception as exc:
            logger.error("Failed to insert episodic turn: %s", exc)

    async def add_execution_step(
        self,
        session_id: str,
        step_name: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Record an autonomous execution step."""
        payload_str = json.dumps(payload or {})
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    "INSERT INTO execution_steps"
                    " (session_id, step_name, status, payload, timestamp)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (session_id, step_name, status, payload_str, time.time()),
                )
        except Exception as exc:
            logger.error("Failed to insert episodic execution step: %s", exc)

    async def delete_session(self, session_id: str) -> None:
        """Delete all records associated with a session."""
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    "DELETE FROM conversation_turns WHERE session_id = ?",
                    (session_id,),
                )
                await conn.execute(
                    "DELETE FROM execution_steps WHERE session_id = ?",
                    (session_id,),
                )
            logger.info("Deleted episodic history for session %s", session_id)
        except Exception as exc:
            logger.error("Failed to delete session history: %s", exc)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_turns(self, session_id: str) -> list[EpisodicTurn]:
        """Fetch all conversation turns for a session in chronological order."""
        turns: list[EpisodicTurn] = []
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT id, session_id, role, content, timestamp, metadata"
                    " FROM conversation_turns"
                    " WHERE session_id = ?"
                    " ORDER BY timestamp ASC",
                    (session_id,),
                )
                rows = await cursor.fetchall()
            for row in rows:
                turns.append(
                    EpisodicTurn(
                        id=row["id"],
                        session_id=row["session_id"],
                        role=row["role"],
                        content=row["content"],
                        timestamp=row["timestamp"],
                        metadata=json.loads(row["metadata"]),
                    )
                )
        except Exception as exc:
            logger.error("Failed to query episodic turns: %s", exc)
        return turns

    async def get_execution_steps(self, session_id: str) -> list[EpisodicStep]:
        """Fetch all execution steps for a session."""
        steps: list[EpisodicStep] = []
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT id, session_id, step_name, status, payload, timestamp"
                    " FROM execution_steps"
                    " WHERE session_id = ?"
                    " ORDER BY timestamp ASC",
                    (session_id,),
                )
                rows = await cursor.fetchall()
            for row in rows:
                steps.append(
                    EpisodicStep(
                        id=row["id"],
                        session_id=row["session_id"],
                        step_name=row["step_name"],
                        status=row["status"],
                        payload=json.loads(row["payload"]),
                        timestamp=row["timestamp"],
                    )
                )
        except Exception as exc:
            logger.error("Failed to query episodic execution steps: %s", exc)
        return steps


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2a: session-scoped episodic memory with migrations and bus integration
# ─────────────────────────────────────────────────────────────────────────────

_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS episodic_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id             TEXT    PRIMARY KEY,
    workspace_root TEXT    NOT NULL,
    started_at     REAL    NOT NULL,
    ended_at       REAL,
    model_used     TEXT,
    mode           TEXT,
    total_tokens   INTEGER DEFAULT 0,
    summary        TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    id           TEXT    PRIMARY KEY,
    session_id   TEXT    NOT NULL REFERENCES sessions(id),
    turn_index   INTEGER NOT NULL,
    role         TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    model_used   TEXT,
    tokens_used  INTEGER,
    created_at   REAL    NOT NULL,
    embedding_id TEXT
);
CREATE TABLE IF NOT EXISTS memory_tags (
    turn_id TEXT NOT NULL REFERENCES turns(id),
    tag     TEXT NOT NULL,
    value   TEXT,
    PRIMARY KEY (turn_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace ON sessions(workspace_root);
CREATE INDEX IF NOT EXISTS idx_sessions_started   ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_turns_session      ON turns(session_id);
CREATE INDEX IF NOT EXISTS idx_turns_created      ON turns(created_at);
CREATE INDEX IF NOT EXISTS idx_turns_role         ON turns(role);
CREATE INDEX IF NOT EXISTS idx_tags_turn          ON memory_tags(turn_id);
CREATE INDEX IF NOT EXISTS idx_tags_tag           ON memory_tags(tag);
"""

_MIGRATIONS: list[tuple[int, str]] = [
    (1, _V1_SCHEMA),
]


class Session(BaseModel):
    """A REPL session record from the episodic sessions table."""

    id: str
    workspace_root: str
    started_at: float
    ended_at: float | None = None
    model_used: str | None = None
    mode: str | None = None
    total_tokens: int = 0
    summary: str | None = None
    first_prompt: str | None = None  # populated via JOIN, not stored in sessions


class Turn(BaseModel):
    """A single conversation turn within an episodic session."""

    id: str
    session_id: str
    turn_index: int
    role: str
    content: str
    model_used: str | None = None
    tokens_used: int | None = None
    created_at: float
    embedding_id: str | None = None


class EpisodicMemory:
    """Phase-2a session-scoped episodic store with migrations and bus integration.

    Sits on top of the same :class:`~velune.memory.storage.sqlite_pool.SQLiteConnectionPool`
    as :class:`EpisodicMemoryTier` but manages its own *sessions / turns / memory_tags*
    schema, versioned via a migration table.

    Lifecycle
    ---------
    Call ``await memory.initialize()`` once (the module system does this via
    the ``lifecycle_key``).  Then:

    * ``start_session()`` at REPL startup
    * ``record_turn()`` per exchange
    * ``end_session()`` on graceful shutdown
    """

    def __init__(self, pool: SQLiteConnectionPool) -> None:
        self._pool = pool

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def initialize(self) -> None:
        await self._apply_migrations()

    async def _apply_migrations(self) -> None:
        async with self._pool.write() as conn:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS episodic_schema_version "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )

        async with self._pool.read() as conn:
            cursor = await conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM episodic_schema_version"
            )
            row = await cursor.fetchone()
        current: int = row[0] if row else 0

        for version, sql in _MIGRATIONS:
            if version > current:
                async with self._pool.write() as conn:
                    for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                        await conn.execute(stmt)
                    await conn.execute(
                        "INSERT INTO episodic_schema_version (version, applied_at) VALUES (?, ?)",
                        (version, time.time()),
                    )
                logger.info("EpisodicMemory: applied schema migration v%d", version)

        logger.debug(
            "EpisodicMemory schema at version %d",
            max(v for v, _ in _MIGRATIONS),
        )

    # ── Session management ───────────────────────────────────────────────────

    async def start_session(self, workspace_root: str, model: str, mode: str) -> str:
        """Create a new session row and return its ID."""
        session_id = f"ses-{uuid.uuid4().hex[:12]}"
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    "INSERT INTO sessions (id, workspace_root, started_at, model_used, mode) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (session_id, workspace_root, time.time(), model, mode),
                )
        except Exception as exc:
            logger.error("Failed to start episodic session: %s", exc)
        logger.debug("EpisodicMemory: started session %s", session_id)
        return session_id

    async def end_session(self, session_id: str) -> None:
        """Close the session, recording end time and summing token usage."""
        try:
            async with self._pool.write() as conn:
                cursor = await conn.execute(
                    "SELECT COALESCE(SUM(tokens_used), 0) FROM turns WHERE session_id = ?",
                    (session_id,),
                )
                row = await cursor.fetchone()
                total_tokens: int = row[0] if row else 0
                await conn.execute(
                    "UPDATE sessions SET ended_at = ?, total_tokens = ? WHERE id = ?",
                    (time.time(), total_tokens, session_id),
                )
        except Exception as exc:
            logger.error("Failed to end episodic session %s: %s", session_id, exc)
        logger.debug("EpisodicMemory: ended session %s", session_id)

    # ── Turn recording ───────────────────────────────────────────────────────

    async def record_turn(
        self,
        session_id: str,
        role: str,
        content: str,
        model: str | None = None,
        tokens: int | None = None,
    ) -> str:
        """Persist a conversation turn and return its ID."""
        turn_id = f"trn-{uuid.uuid4().hex[:12]}"
        try:
            # The index must be derived inside the write transaction. Reading it
            # from a separate read connection first let two concurrent
            # record_turn calls — the user and assistant halves of one turn are
            # both fire-and-forget — observe the same COUNT and insert duplicate
            # turn_index values, which permanently corrupts ordering on replay.
            async with self._pool.write() as conn:
                await conn.execute(
                    "INSERT INTO turns "
                    "(id, session_id, turn_index, role, content, model_used, tokens_used, created_at) "
                    "SELECT ?, ?, COALESCE(MAX(turn_index) + 1, 0), ?, ?, ?, ?, ? "
                    "FROM turns WHERE session_id = ?",
                    (
                        turn_id,
                        session_id,
                        role,
                        content,
                        model,
                        tokens,
                        time.time(),
                        session_id,
                    ),
                )
        except Exception as exc:
            logger.error("Failed to record episodic turn: %s", exc)
        return turn_id

    # ── Read operations ──────────────────────────────────────────────────────

    async def get_recent_turns(self, session_id: str, limit: int = 20) -> list[Turn]:
        """Return the *limit* most recent turns in chronological order."""
        turns: list[Turn] = []
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT id, session_id, turn_index, role, content, "
                    "model_used, tokens_used, created_at, embedding_id "
                    "FROM turns WHERE session_id = ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (session_id, limit),
                )
                rows = await cursor.fetchall()
            for row in reversed(rows):
                turns.append(_row_to_turn(row))
        except Exception as exc:
            logger.error("Failed to query recent turns: %s", exc)
        return turns

    async def get_session_history(self, session_id: str) -> list[Turn]:
        """Return all turns for a session in chronological order."""
        turns: list[Turn] = []
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT id, session_id, turn_index, role, content, "
                    "model_used, tokens_used, created_at, embedding_id "
                    "FROM turns WHERE session_id = ? ORDER BY created_at ASC",
                    (session_id,),
                )
                rows = await cursor.fetchall()
            for row in rows:
                turns.append(_row_to_turn(row))
        except Exception as exc:
            logger.error("Failed to query session history: %s", exc)
        return turns

    async def search_by_content(
        self, query: str, workspace_root: str, limit: int = 10
    ) -> list[Turn]:
        """LIKE-based content search across all turns in *workspace_root*."""
        turns: list[Turn] = []
        try:
            pattern = f"%{query}%"
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT t.id, t.session_id, t.turn_index, t.role, t.content, "
                    "t.model_used, t.tokens_used, t.created_at, t.embedding_id "
                    "FROM turns t JOIN sessions s ON t.session_id = s.id "
                    "WHERE s.workspace_root = ? AND t.content LIKE ? "
                    "ORDER BY t.created_at DESC LIMIT ?",
                    (workspace_root, pattern, limit),
                )
                rows = await cursor.fetchall()
            for row in rows:
                turns.append(_row_to_turn(row))
        except Exception as exc:
            logger.error("Failed to search turns by content: %s", exc)
        return turns

    async def list_recent_sessions(self, workspace_root: str, limit: int = 20) -> list[Session]:
        """Return the *limit* most recent sessions for *workspace_root*."""
        sessions: list[Session] = []
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT s.id, s.workspace_root, s.started_at, s.ended_at, "
                    "s.model_used, s.mode, s.total_tokens, s.summary, "
                    "(SELECT content FROM turns "
                    " WHERE session_id = s.id AND role = 'user' "
                    " ORDER BY created_at ASC LIMIT 1) AS first_prompt "
                    "FROM sessions s "
                    "WHERE s.workspace_root = ? "
                    "ORDER BY s.started_at DESC LIMIT ?",
                    (workspace_root, limit),
                )
                rows = await cursor.fetchall()
            for row in rows:
                sessions.append(
                    Session(
                        id=row["id"],
                        workspace_root=row["workspace_root"],
                        started_at=row["started_at"],
                        ended_at=row["ended_at"],
                        model_used=row["model_used"],
                        mode=row["mode"],
                        total_tokens=row["total_tokens"] or 0,
                        summary=row["summary"],
                        first_prompt=row["first_prompt"],
                    )
                )
        except Exception as exc:
            logger.error("Failed to list recent sessions: %s", exc)
        return sessions

    async def get_session_summary(self, session_id: str) -> str | None:
        """Return the stored LLM-generated summary for *session_id*, or None."""
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT summary FROM sessions WHERE id = ?", (session_id,)
                )
                row = await cursor.fetchone()
            return row["summary"] if row else None
        except Exception as exc:
            logger.error("Failed to get session summary: %s", exc)
            return None

    async def set_session_summary(self, session_id: str, summary: str) -> None:
        """Persist an LLM-generated summary for *session_id*."""
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    "UPDATE sessions SET summary = ? WHERE id = ?",
                    (summary, session_id),
                )
        except Exception as exc:
            logger.error("Failed to set session summary: %s", exc)


# ── Private helpers ───────────────────────────────────────────────────────────


def _row_to_turn(row: Any) -> Turn:
    return Turn(
        id=row["id"],
        session_id=row["session_id"],
        turn_index=row["turn_index"],
        role=row["role"],
        content=row["content"],
        model_used=row["model_used"],
        tokens_used=row["tokens_used"],
        created_at=row["created_at"],
        embedding_id=row["embedding_id"],
    )
