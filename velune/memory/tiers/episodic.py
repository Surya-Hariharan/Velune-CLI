"""Episodic Memory Tier (Tier 2).

SQLite-backed store for structured historical reads of execution steps,
intermediate tool outputs, and complete conversation traces.

All I/O is async and routed through :class:`~velune.memory.storage.sqlite_pool.SQLiteConnectionPool`
so concurrent coroutines never cause WAL write contention.
"""

from __future__ import annotations

import json
import logging
import time
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
                turns.append(EpisodicTurn(
                    id=row["id"],
                    session_id=row["session_id"],
                    role=row["role"],
                    content=row["content"],
                    timestamp=row["timestamp"],
                    metadata=json.loads(row["metadata"]),
                ))
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
                steps.append(EpisodicStep(
                    id=row["id"],
                    session_id=row["session_id"],
                    step_name=row["step_name"],
                    status=row["status"],
                    payload=json.loads(row["payload"]),
                    timestamp=row["timestamp"],
                ))
        except Exception as exc:
            logger.error("Failed to query episodic execution steps: %s", exc)
        return steps
