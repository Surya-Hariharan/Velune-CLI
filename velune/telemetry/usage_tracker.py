"""Token and cost tracking for sessions.

Tracks:
- Token usage per model
- Estimated costs per session
- Cross-session analytics
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Optional

import structlog

logger = structlog.get_logger()

# Cost estimates per 1M tokens (approximate)
DEFAULT_COSTS = {
    "claude-opus": {"input": 15.0, "output": 75.0},
    "claude-sonnet": {"input": 3.0, "output": 15.0},
    "claude-haiku": {"input": 0.80, "output": 4.0},
    "gpt-4": {"input": 30.0, "output": 60.0},
    "gpt-3.5-turbo": {"input": 0.50, "output": 1.50},
}


@dataclass
class UsageRecord:
    """Single record of model usage."""

    session_id: str
    timestamp: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost: float | None = None


@dataclass
class UsageSummary:
    """Summary of usage for a session."""

    session_id: str
    total_tokens: int
    total_cost: float | None
    total_input_tokens: int
    total_output_tokens: int
    model_breakdown: dict[str, int]  # {model: total_tokens}
    record_count: int
    start_time: str
    end_time: str


class SessionUsageTracker:
    """Tracks token and cost usage per session."""

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize tracker with SQLite database.

        Args:
            db_path: Path to SQLite database (default: ~/.velune/telemetry/usage.db)
        """
        if db_path is None:
            db_path = Path.home() / ".velune" / "telemetry" / "usage.db"

        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._lock = Lock()

        # Initialize database
        self._init_db()

    def _init_db(self) -> None:
        """Initialize SQLite schema."""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_records (
                    id INTEGER PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    output_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    estimated_cost REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_id ON usage_records(session_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_timestamp ON usage_records(timestamp)"
            )
            conn.commit()

    def record_completion(
        self,
        session_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Record a model completion.

        Args:
            session_id: Session identifier
            model: Model name (e.g., "claude-opus")
            input_tokens: Input tokens used
            output_tokens: Output tokens used
        """
        total_tokens = input_tokens + output_tokens
        estimated_cost = self._estimate_cost(model, input_tokens, output_tokens)

        record = UsageRecord(
            session_id=session_id,
            timestamp=datetime.utcnow().isoformat(),
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            estimated_cost=estimated_cost,
        )

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO usage_records
                    (session_id, timestamp, model, input_tokens, output_tokens, total_tokens, estimated_cost)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.session_id,
                        record.timestamp,
                        record.model,
                        record.input_tokens,
                        record.output_tokens,
                        record.total_tokens,
                        record.estimated_cost,
                    ),
                )
                conn.commit()

        logger.debug(
            "Usage recorded",
            session_id=session_id,
            model=model,
            tokens=total_tokens,
            cost=estimated_cost,
        )

    def get_session_total_tokens(self, session_id: str) -> int:
        """Get total tokens used in a session."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT SUM(total_tokens) FROM usage_records WHERE session_id = ?",
                (session_id,),
            )
            result = cursor.fetchone()
            return result[0] if result and result[0] else 0

    def get_session_estimated_cost(self, session_id: str) -> float | None:
        """Get estimated total cost for a session."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT SUM(estimated_cost) FROM usage_records WHERE session_id = ?",
                (session_id,),
            )
            result = cursor.fetchone()
            return result[0] if result and result[0] else None

    def get_session_summary(self, session_id: str) -> UsageSummary | None:
        """Get complete summary for a session."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT
                    SUM(total_tokens) as total_tokens,
                    SUM(estimated_cost) as total_cost,
                    SUM(input_tokens) as input_tokens,
                    SUM(output_tokens) as output_tokens,
                    COUNT(*) as record_count,
                    MIN(timestamp) as start_time,
                    MAX(timestamp) as end_time
                FROM usage_records
                WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cursor.fetchone()

            if not row or row[0] is None:
                return None

            # Get model breakdown
            cursor = conn.execute(
                """
                SELECT model, SUM(total_tokens)
                FROM usage_records
                WHERE session_id = ?
                GROUP BY model
                """,
                (session_id,),
            )
            model_breakdown = {row[0]: row[1] for row in cursor.fetchall()}

            return UsageSummary(
                session_id=session_id,
                total_tokens=row[0],
                total_cost=row[1],
                total_input_tokens=row[2],
                total_output_tokens=row[3],
                model_breakdown=model_breakdown,
                record_count=row[4],
                start_time=row[5],
                end_time=row[6],
            )

    def get_recent_sessions(self, days: int = 7) -> list[UsageSummary]:
        """Get summaries for sessions in the last N days."""
        cutoff = datetime.utcnow() - timedelta(days=days)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT DISTINCT session_id
                FROM usage_records
                WHERE timestamp > ?
                ORDER BY timestamp DESC
                """,
                (cutoff.isoformat(),),
            )

            sessions = [row[0] for row in cursor.fetchall()]

        summaries = []
        for session_id in sessions:
            summary = self.get_session_summary(session_id)
            if summary:
                summaries.append(summary)

        return summaries

    def get_stats_last_n_days(self, days: int = 7) -> dict[str, any]:
        """Get aggregated stats for last N days."""
        cutoff = datetime.utcnow() - timedelta(days=days)

        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT
                    COUNT(DISTINCT session_id) as session_count,
                    SUM(total_tokens) as total_tokens,
                    SUM(estimated_cost) as total_cost,
                    COUNT(*) as completion_count
                FROM usage_records
                WHERE timestamp > ?
                """,
                (cutoff.isoformat(),),
            )
            row = cursor.fetchone()

            cursor = conn.execute(
                """
                SELECT model, SUM(total_tokens)
                FROM usage_records
                WHERE timestamp > ?
                GROUP BY model
                ORDER BY SUM(total_tokens) DESC
                LIMIT 1
                """,
                (cutoff.isoformat(),),
            )
            most_used = cursor.fetchone()

        return {
            "days": days,
            "session_count": row[0] if row else 0,
            "total_tokens": row[1] if row and row[1] else 0,
            "total_cost": row[2] if row and row[2] else 0,
            "completion_count": row[3] if row else 0,
            "most_used_model": most_used[0] if most_used else None,
            "most_used_model_tokens": most_used[1] if most_used else 0,
        }

    def _estimate_cost(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
    ) -> float | None:
        """Estimate cost for a completion.

        Uses DEFAULT_COSTS table. Returns None if model not found.
        """
        # Normalize model name
        model_lower = model.lower()
        for key in DEFAULT_COSTS:
            if key.lower() in model_lower or model_lower in key.lower():
                costs = DEFAULT_COSTS[key]
                input_cost = (input_tokens / 1_000_000) * costs["input"]
                output_cost = (output_tokens / 1_000_000) * costs["output"]
                return input_cost + output_cost

        # Unknown model
        return None

    def cleanup_old_records(self, days: int = 90) -> int:
        """Delete records older than N days. Returns count deleted."""
        cutoff = datetime.utcnow() - timedelta(days=days)

        with self._lock:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute(
                    "DELETE FROM usage_records WHERE timestamp < ?",
                    (cutoff.isoformat(),),
                )
                count = cursor.rowcount
                conn.commit()

        if count > 0:
            logger.info("Cleaned up old usage records", count=count, days_old=days)

        return count


# Global instance
_tracker: SessionUsageTracker | None = None


def get_tracker() -> SessionUsageTracker:
    """Get or create global usage tracker instance."""
    global _tracker
    if _tracker is None:
        _tracker = SessionUsageTracker()
    return _tracker
