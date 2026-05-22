"""Checkpoint persistence for resumable orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
import sqlite3
import json
from pathlib import Path
from typing import Any

from velune.orchestration.schemas import OrchestrationState


@dataclass(slots=True)
class CheckpointRecord:
    """Snapshot record for a graph transition boundary."""

    checkpoint_id: str
    run_id: str
    node_name: str
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    state: OrchestrationState | None = None


class InMemoryCheckpointStore:
    """Stores orchestration checkpoints for resume and diagnostics in memory."""

    def __init__(self) -> None:
        self._records: dict[str, list[CheckpointRecord]] = {}

    def save(self, run_id: str, node_name: str, state: OrchestrationState) -> str:
        sequence = len(self._records.get(run_id, [])) + 1
        checkpoint_id = f"{run_id}:cp:{sequence:04d}"
        record = CheckpointRecord(
            checkpoint_id=checkpoint_id,
            run_id=run_id,
            node_name=node_name,
            state=deepcopy(state),
        )
        self._records.setdefault(run_id, []).append(record)
        return checkpoint_id

    def latest(self, run_id: str) -> OrchestrationState | None:
        records = self._records.get(run_id, [])
        if not records:
            return None
        return deepcopy(records[-1].state)

    def list_ids(self, run_id: str) -> list[str]:
        return [record.checkpoint_id for record in self._records.get(run_id, [])]

    def load(self, run_id: str, checkpoint_id: str) -> OrchestrationState | None:
        for record in self._records.get(run_id, []):
            if record.checkpoint_id == checkpoint_id:
                return deepcopy(record.state)
        return None


class SQLiteCheckpointStore:
    """Stores orchestration checkpoints in a local SQLite database for crash resilience and resume capabilities."""

    def __init__(self, db_path: str | Path | None = None) -> None:
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
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    node_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    state_json TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_checkpoints_run_id ON checkpoints(run_id)
            """)
            conn.commit()

    def save(self, run_id: str, node_name: str, state: OrchestrationState) -> str:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM checkpoints WHERE run_id = ?", (run_id,))
            count = cursor.fetchone()[0]
            
            sequence = count + 1
            checkpoint_id = f"{run_id}:cp:{sequence:04d}"
            
            created_at = datetime.now(tz=UTC).isoformat()
            
            # Serialize OrchestrationState using pydantic support
            if hasattr(state, "model_dump_json"):
                state_json = state.model_dump_json()
            else:
                state_json = state.json()

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
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT state_json FROM checkpoints WHERE run_id = ? ORDER BY checkpoint_id DESC LIMIT 1",
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
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT checkpoint_id FROM checkpoints WHERE run_id = ? ORDER BY checkpoint_id ASC",
                (run_id,),
            )
            return [row["checkpoint_id"] for row in cursor.fetchall()]

    def load(self, run_id: str, checkpoint_id: str) -> OrchestrationState | None:
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

