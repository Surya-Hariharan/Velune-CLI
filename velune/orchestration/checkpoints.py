"""Checkpoint persistence for resumable orchestration."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from velune.orchestration.schemas import OrchestrationState

logger = logging.getLogger("velune.orchestration.checkpoints")


@dataclass(slots=True)
class CheckpointRecord:
    """Snapshot record for a graph transition boundary."""

    checkpoint_id: str
    run_id: str
    node_name: str
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    state: OrchestrationState | None = None

class SQLiteCheckpointStore:
    """Stores orchestration checkpoints in a local SQLite database for crash resilience and resume capabilities."""

    def __init__(self, db_path: str | Path | None = None, sqlite_manager: Any = None) -> None:
        self.sqlite_manager = sqlite_manager
        if self.sqlite_manager is not None:
            self.db_path = self.sqlite_manager.db_path
        else:
            if db_path is None:
                self.db_path = Path(".velune") / "velune_cognitive_core.db"
            else:
                self.db_path = Path(db_path)

            # Ensure the directory exists
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        create_table_query = """
            CREATE TABLE IF NOT EXISTS checkpoints (
                checkpoint_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                node_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                state_json TEXT NOT NULL
            )
        """
        create_index_query = """
            CREATE INDEX IF NOT EXISTS idx_checkpoints_run_id ON checkpoints(run_id)
        """
        if self.sqlite_manager is not None:
            try:
                self.sqlite_manager.execute_script(create_table_query)
                self.sqlite_manager.execute_script(create_index_query)
            except (TimeoutError, RuntimeError) as e:
                logger.critical("Failed to initialize database schema: %s", e)
                raise
        else:
            with self._get_connection() as conn:
                conn.execute(create_table_query)
                conn.execute(create_index_query)
                conn.commit()

    def save(self, run_id: str, node_name: str, state: OrchestrationState) -> str:
        checkpoint_id = f"{run_id}:cp:{node_name}:{uuid.uuid4().hex[:8]}"
        created_at = datetime.now(tz=UTC).isoformat()

        # Serialize OrchestrationState using pydantic support
        if hasattr(state, "model_dump_json"):
            state_json = state.model_dump_json()
        else:
            state_json = state.json()

        if self.sqlite_manager is not None:
            try:
                self.sqlite_manager.execute_write_sync(
                    """
                    INSERT INTO checkpoints (checkpoint_id, run_id, node_name, created_at, state_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (checkpoint_id, run_id, node_name, created_at, state_json),
                )
            except (TimeoutError, RuntimeError) as e:
                logger.error("Checkpoint save failed: %s", e)
                raise
            return checkpoint_id
        else:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO checkpoints (checkpoint_id, run_id, node_name, created_at, state_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (checkpoint_id, run_id, node_name, created_at, state_json),
                )
                conn.commit()
                return checkpoint_id

    def latest(self, run_id: str) -> OrchestrationState | None:
        if self.sqlite_manager is not None:
            rows = self.sqlite_manager.execute_read(
                "SELECT state_json FROM checkpoints WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                (run_id,),
            )
            if not rows:
                return None
            state_json = rows[0]["state_json"]
            if hasattr(OrchestrationState, "model_validate_json"):
                return OrchestrationState.model_validate_json(state_json)
            else:
                return OrchestrationState.parse_raw(state_json)
        else:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT state_json FROM checkpoints WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                    (run_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                state_json = row["state_json"]
                if hasattr(OrchestrationState, "model_validate_json"):
                    return OrchestrationState.model_validate_json(state_json)
                else:
                    return OrchestrationState.parse_raw(state_json)

    def list_ids(self, run_id: str) -> list[str]:
        if self.sqlite_manager is not None:
            rows = self.sqlite_manager.execute_read(
                "SELECT checkpoint_id FROM checkpoints WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            )
            return [row["checkpoint_id"] for row in rows]
        else:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT checkpoint_id FROM checkpoints WHERE run_id = ? ORDER BY created_at ASC",
                    (run_id,),
                )
                return [row["checkpoint_id"] for row in cursor.fetchall()]

    def load(self, run_id: str, checkpoint_id: str) -> OrchestrationState | None:
        if self.sqlite_manager is not None:
            rows = self.sqlite_manager.execute_read(
                "SELECT state_json FROM checkpoints WHERE run_id = ? AND checkpoint_id = ?",
                (run_id, checkpoint_id),
            )
            if not rows:
                return None
            state_json = rows[0]["state_json"]
            if hasattr(OrchestrationState, "model_validate_json"):
                return OrchestrationState.model_validate_json(state_json)
            else:
                return OrchestrationState.parse_raw(state_json)
        else:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT state_json FROM checkpoints WHERE run_id = ? AND checkpoint_id = ?",
                    (run_id, checkpoint_id),
                )
                row = cursor.fetchone()
                if not row:
                    return None
                state_json = row["state_json"]
                if hasattr(OrchestrationState, "model_validate_json"):
                    return OrchestrationState.model_validate_json(state_json)
                else:
                    return OrchestrationState.parse_raw(state_json)

