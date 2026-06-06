"""Cognitive Performance Analytics and Dynamic Model Routing."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.memory.storage.sqlite_manager import SQLiteManager

logger = logging.getLogger("velune.telemetry.cognition")


class CognitivePerformanceAnalytics:
    """Tracks and persists cognitive metrics like hallucination rates, rollbacks, and routes dynamically.

    Routes all reads and writes through SQLiteManager to guarantee thread safety and eliminate lock contention.
    """

    def __init__(self, sqlite_manager: SQLiteManager | None = None, db_path: str | Path | None = None) -> None:
        if sqlite_manager is not None:
            self.sqlite_manager = sqlite_manager
            self.db_path = sqlite_manager.db_path
        else:
            # Standalone mode (doctor command, tests)
            if db_path is None:
                self.db_path = Path(".velune") / "velune_cognitive_core.db"
            else:
                self.db_path = Path(db_path)

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            from velune.memory.storage.sqlite_manager import SQLiteManager
            self.sqlite_manager = SQLiteManager(self.db_path)

        self._init_db()

    def _init_db(self) -> None:
        script = """
            CREATE TABLE IF NOT EXISTS cognitive_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                model_id TEXT NOT NULL,
                task_type TEXT NOT NULL,
                language TEXT,
                directory TEXT,
                hallucinated INTEGER NOT NULL, -- 0 or 1
                rolled_back INTEGER NOT NULL,  -- 0 or 1
                token_count INTEGER NOT NULL,
                execution_time_ms INTEGER NOT NULL,
                success INTEGER NOT NULL       -- 0 or 1
            );
            CREATE TABLE IF NOT EXISTS critic_performance_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                critic_role TEXT NOT NULL,
                vote INTEGER NOT NULL,      -- 1 for passed, 0 for objected
                success INTEGER NOT NULL    -- 1 for success, 0 for failed
            );
            CREATE TABLE IF NOT EXISTS debate_telemetry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                turns_required INTEGER NOT NULL,
                initial_objection_count INTEGER NOT NULL,
                final_objection_count INTEGER NOT NULL,
                converged INTEGER NOT NULL,
                time_to_converge_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS compression_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                original_tokens INTEGER NOT NULL,
                compressed_tokens INTEGER NOT NULL,
                method TEXT NOT NULL,
                latency_ms INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS injection_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                source TEXT NOT NULL,
                pattern TEXT NOT NULL
            );
        """
        try:
            self.sqlite_manager.execute_script(script)
        except (TimeoutError, RuntimeError) as e:
            logger.critical("Failed to initialize database schema: %s", e)
            raise

    def record_metrics(
        self,
        model_id: str,
        task_type: str,
        hallucinated: bool,
        rolled_back: bool,
        token_count: int,
        execution_time_ms: int,
        success: bool,
        language: str | None = None,
        directory: str | None = None,
    ) -> None:
        """Records a single cognitive invocation metric."""
        query = """
            INSERT INTO cognitive_metrics (
                timestamp, model_id, task_type, language, directory,
                hallucinated, rolled_back, token_count, execution_time_ms, success
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            datetime.now(tz=UTC).isoformat(),
            model_id,
            task_type,
            language,
            directory,
            1 if hallucinated else 0,
            1 if rolled_back else 0,
            token_count,
            execution_time_ms,
            1 if success else 0,
        )
        self.sqlite_manager.execute_write(query, params)
        logger.debug("Recorded cognitive metrics for model %s", model_id)

    def get_model_performance(self, model_id: str) -> dict[str, Any]:
        """Calculates performance aggregated KPIs for a specific model."""
        query = """
            SELECT
                COUNT(*) as total_runs,
                SUM(hallucinated) as total_hallucinations,
                SUM(rolled_back) as total_rollbacks,
                SUM(success) as total_successes,
                AVG(token_count) as avg_tokens,
                AVG(execution_time_ms) as avg_execution_time
            FROM cognitive_metrics
            WHERE model_id = ?
        """
        rows = self.sqlite_manager.execute_read(query, (model_id,))
        if not rows or rows[0]["total_runs"] == 0:
            return {
                "total_runs": 0,
                "hallucination_rate": 0.0,
                "rollback_ratio": 0.0,
                "success_rate": 0.0,
                "avg_tokens": 0.0,
                "avg_execution_time": 0.0,
            }

        row = rows[0]
        total = row["total_runs"]
        return {
            "total_runs": total,
            "hallucination_rate": round(float(row["total_hallucinations"] or 0) / total, 3),
            "rollback_ratio": round(float(row["total_rollbacks"] or 0) / total, 3),
            "success_rate": round(float(row["total_successes"] or 0) / total, 3),
            "avg_tokens": round(float(row["avg_tokens"] or 0), 1),
            "avg_execution_time": round(float(row["avg_execution_time"] or 0), 1),
        }

    def record_critic_vote(
        self,
        task_id: str,
        critic_role: str,
        vote: bool,
        success: bool,
    ) -> None:
        """Records a single critic vote and the eventual execution success."""
        query = """
            INSERT INTO critic_performance_metrics (task_id, critic_role, vote, success)
            VALUES (?, ?, ?, ?)
        """
        params = (task_id, critic_role, 1 if vote else 0, 1 if success else 0)
        self.sqlite_manager.execute_write(query, params)
        logger.debug("Recorded critic vote for task %s, role %s", task_id, critic_role)

    def get_critic_weights(self) -> dict[str, float]:
        """Returns a static default weight map for council critics."""
        return {
            "scalability": 1.0,
            "security": 1.0,
            "performance": 1.0,
            "maintainability": 1.0,
        }

    def record_debate_outcome(
        self,
        turns_required: int,
        initial_objection_count: int,
        final_objection_count: int,
        converged: bool,
        time_to_converge_ms: int,
    ) -> None:
        """Records a single debate's performance and convergence telemetry."""
        query = """
            INSERT INTO debate_telemetry (
                timestamp, turns_required, initial_objection_count,
                final_objection_count, converged, time_to_converge_ms
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """
        params = (
            datetime.now(tz=UTC).isoformat(),
            turns_required,
            initial_objection_count,
            final_objection_count,
            1 if converged else 0,
            time_to_converge_ms,
        )
        self.sqlite_manager.execute_write(query, params)
        logger.debug(
            "Recorded debate outcome: turns=%d, converged=%s, initial_objections=%d",
            turns_required,
            converged,
            initial_objection_count,
        )

    def record_compression(
        self,
        original_tokens: int,
        compressed_tokens: int,
        method: str,
        latency_ms: int,
    ) -> None:
        """Records a single context compression invocation's telemetry."""
        query = """
            INSERT INTO compression_metrics (
                timestamp, original_tokens, compressed_tokens,
                method, latency_ms
            )
            VALUES (?, ?, ?, ?, ?)
        """
        params = (
            datetime.now(tz=UTC).isoformat(),
            original_tokens,
            compressed_tokens,
            method,
            latency_ms,
        )
        self.sqlite_manager.execute_write(query, params)
        logger.debug(
            "Recorded compression metrics: original_tokens=%d, compressed_tokens=%d, method=%s",
            original_tokens,
            compressed_tokens,
            method,
        )

    def record_injection_attempt(self, source: str, pattern: str) -> None:
        """Records a single blocked prompt injection attempt."""
        query = """
            INSERT INTO injection_attempts (timestamp, source, pattern)
            VALUES (?, ?, ?)
        """
        params = (
            datetime.now(tz=UTC).isoformat(),
            source,
            pattern,
        )
        self.sqlite_manager.execute_write(query, params)
        logger.warning("Recorded prompt injection attempt from source '%s' matching pattern: %s", source, pattern)
