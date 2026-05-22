"""Cognitive Performance Analytics and Dynamic Model Routing."""

from __future__ import annotations

import sqlite3
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import UTC, datetime

logger = logging.getLogger("velune.telemetry.cognition")


class CognitivePerformanceAnalytics:
    """Tracks and persists cognitive metrics like hallucination rates, rollbacks, and routes dynamically."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            self.db_path = Path(".velune") / "velune_cognitive_core.db"
        else:
            self.db_path = Path(db_path)
            
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            # Create metrics table
            conn.execute("""
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
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS critic_performance_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    critic_role TEXT NOT NULL,
                    vote INTEGER NOT NULL,      -- 1 for passed, 0 for objected
                    success INTEGER NOT NULL    -- 1 for success, 0 for failed
                )
            """)
            conn.commit()

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
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO cognitive_metrics (
                    timestamp, model_id, task_type, language, directory,
                    hallucinated, rolled_back, token_count, execution_time_ms, success
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
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
                ),
            )
            conn.commit()
            logger.debug("Recorded cognitive metrics for model %s", model_id)

    def get_model_performance(self, model_id: str) -> Dict[str, Any]:
        """Calculates performance aggregated KPIs for a specific model."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT 
                    COUNT(*) as total_runs,
                    SUM(hallucinated) as total_hallucinations,
                    SUM(rolled_back) as total_rollbacks,
                    SUM(success) as total_successes,
                    AVG(token_count) as avg_tokens,
                    AVG(execution_time_ms) as avg_execution_time
                FROM cognitive_metrics
                WHERE model_id = ?
                """,
                (model_id,),
            )
            row = cursor.fetchone()
            if not row or row["total_runs"] == 0:
                return {
                    "total_runs": 0,
                    "hallucination_rate": 0.0,
                    "rollback_ratio": 0.0,
                    "success_rate": 0.0,
                    "avg_tokens": 0.0,
                    "avg_execution_time": 0.0,
                }
            
            total = row["total_runs"]
            return {
                "total_runs": total,
                "hallucination_rate": round(float(row["total_hallucinations"] or 0) / total, 3),
                "rollback_ratio": round(float(row["total_rollbacks"] or 0) / total, 3),
                "success_rate": round(float(row["total_successes"] or 0) / total, 3),
                "avg_tokens": round(float(row["avg_tokens"] or 0), 1),
                "avg_execution_time": round(float(row["avg_execution_time"] or 0), 1),
            }

    def route_reasoning_task(
        self,
        task_type: str,
        available_models: List[str],
        language: str | None = None,
        directory: str | None = None,
        default_model: str | None = None,
    ) -> str:
        """
        Dynamically routes task to the best registered model based on historical metrics
        (lowest rollback ratio, lowest hallucination rate, and highest success rate).
        """
        if not available_models:
            if default_model:
                return default_model
            raise ValueError("No available models to route to.")

        if len(available_models) == 1:
            return available_models[0]

        best_model = None
        best_score = -1.0

        for model in available_models:
            perf = self.get_model_performance(model)
            if perf["total_runs"] == 0:
                # Give unmeasured models a neutral baseline score
                score = 0.5
            else:
                # Higher success rate, lower hallucination/rollback rate is better
                score = (
                    perf["success_rate"] * 0.5
                    + (1.0 - perf["hallucination_rate"]) * 0.25
                    + (1.0 - perf["rollback_ratio"]) * 0.25
                )
            
            if score > best_score:
                best_score = score
                best_model = model

        return best_model or available_models[0]

    def record_critic_vote(
        self,
        task_id: str,
        critic_role: str,
        vote: bool,
        success: bool,
    ) -> None:
        """Records a single critic vote and the eventual execution success."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO critic_performance_metrics (task_id, critic_role, vote, success)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, critic_role, 1 if vote else 0, 1 if success else 0),
            )
            conn.commit()
            logger.debug("Recorded critic vote for task %s, role %s", task_id, critic_role)

    def get_critic_weights(self) -> Dict[str, float]:
        """
        Executes the reinforcement learning weight update formula over historical runs.
        Returns a dictionary mapping critic roles to tuned weights bounded in [0.1, 2.0].
        """
        weights = {
            "scalability": 1.0,
            "security": 1.0,
            "performance": 1.0,
            "maintainability": 1.0,
        }
        eta = 0.05

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT critic_role, vote, success 
                FROM critic_performance_metrics 
                ORDER BY id ASC
                """
            )
            rows = cursor.fetchall()
            for row in rows:
                role = row["critic_role"].lower()
                if role not in weights:
                    weights[role] = 1.0
                
                vote = float(row["vote"])  # 1.0 or 0.0
                success = float(row["success"])  # 1.0 or 0.0
                
                # Formula: W_t+1 = W_t + eta * (success * (vote - 0.5) - (1.0 - success) * (vote - 0.5))
                delta = eta * (success * (vote - 0.5) - (1.0 - success) * (vote - 0.5))
                weights[role] = max(0.1, min(2.0, weights[role] + delta))
                
        return weights
