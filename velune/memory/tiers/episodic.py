"""Episodic Memory Tier (Tier 2).

SQLite-backed store for structured historical reads of execution steps,
intermediate tool outputs, and complete conversation traces.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

from velune.memory.storage.sqlite_manager import SQLiteManager

logger = logging.getLogger("velune.memory.tiers.episodic")


class EpisodicTurn(BaseModel):
    """An episodic conversation turn stored in SQLite."""
    id: Optional[int] = None
    session_id: str
    role: str
    content: str
    timestamp: float
    metadata: Dict[str, Any] = Field(default_factory=dict)


class EpisodicStep(BaseModel):
    """An episodic execution step stored in SQLite."""
    id: Optional[int] = None
    session_id: str
    step_name: str
    status: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    timestamp: float


class EpisodicMemoryTier:
    """Tier 2: Persisted SQLite storage for local episodic trace retrieval."""

    def __init__(self, db_path: Path, sqlite_manager: Optional[SQLiteManager] = None) -> None:
        self.db_path = db_path
        self.sqlite_manager = sqlite_manager or SQLiteManager(db_path)
        self._initialized = False
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite database schemas."""
        try:
            schema_sql = """
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
            self.sqlite_manager.execute_script(schema_sql)
            self._initialized = True
            logger.info("Successfully initialized Episodic SQLite DB via SQLiteManager")
        except Exception as e:
            logger.error("Failed to initialize Episodic SQLite DB: %s", e)

    def add_turn(self, session_id: str, role: str, content: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        """Add a conversation turn to SQLite episodic memory."""
        meta_str = json.dumps(metadata or {})
        now = time.time()
        try:
            self.sqlite_manager.execute_write(
                "INSERT INTO conversation_turns (session_id, role, content, timestamp, metadata) VALUES (?, ?, ?, ?, ?)",
                (session_id, role, content, now, meta_str)
            )
        except Exception as e:
            logger.error("Failed to insert episodic turn: %s", e)

    def get_turns(self, session_id: str) -> List[EpisodicTurn]:
        """Fetch all conversation turns for a session in chronological order."""
        turns: List[EpisodicTurn] = []
        try:
            rows = self.sqlite_manager.execute_read(
                "SELECT id, session_id, role, content, timestamp, metadata FROM conversation_turns WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,)
            )
            for row in rows:
                turns.append(EpisodicTurn(
                    id=row["id"],
                    session_id=row["session_id"],
                    role=row["role"],
                    content=row["content"],
                    timestamp=row["timestamp"],
                    metadata=json.loads(row["metadata"]),
                ))
        except Exception as e:
            logger.error("Failed to query episodic turns: %s", e)
        return turns

    def add_execution_step(self, session_id: str, step_name: str, status: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """Record an autonomous execution step to SQLite episodic memory."""
        payload_str = json.dumps(payload or {})
        now = time.time()
        try:
            self.sqlite_manager.execute_write(
                "INSERT INTO execution_steps (session_id, step_name, status, payload, timestamp) VALUES (?, ?, ?, ?, ?)",
                (session_id, step_name, status, payload_str, now)
            )
        except Exception as e:
            logger.error("Failed to insert episodic execution step: %s", e)

    def get_execution_steps(self, session_id: str) -> List[EpisodicStep]:
        """Fetch all execution steps for a session."""
        steps: List[EpisodicStep] = []
        try:
            rows = self.sqlite_manager.execute_read(
                "SELECT id, session_id, step_name, status, payload, timestamp FROM execution_steps WHERE session_id = ? ORDER BY timestamp ASC",
                (session_id,)
            )
            for row in rows:
                steps.append(EpisodicStep(
                    id=row["id"],
                    session_id=row["session_id"],
                    step_name=row["step_name"],
                    status=row["status"],
                    payload=json.loads(row["payload"]),
                    timestamp=row["timestamp"],
                ))
        except Exception as e:
            logger.error("Failed to query episodic execution steps: %s", e)
        return steps

    def delete_session(self, session_id: str) -> None:
        """Delete all records associated with a session."""
        try:
            self.sqlite_manager.execute_write("DELETE FROM conversation_turns WHERE session_id = ?", (session_id,))
            self.sqlite_manager.execute_write("DELETE FROM execution_steps WHERE session_id = ?", (session_id,))
            logger.info("Deleted episodic history for session %s", session_id)
        except Exception as e:
            logger.error("Failed to delete session history: %s", e)
