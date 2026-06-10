"""Decision Lineage and Failed Experiment Memory Tier.

SQLite-backed persistent store for long-running cognitive continuity and
failed-approach blocking.

All I/O is async and routed through
:class:`~velune.memory.storage.sqlite_pool.SQLiteConnectionPool` so
concurrent coroutines never cause WAL write contention.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from velune.memory.storage.sqlite_pool import SQLiteConnectionPool

logger = logging.getLogger("velune.memory.tiers.lineage")

_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS decision_nodes (
        id TEXT PRIMARY KEY,
        timestamp REAL NOT NULL,
        target_subsystem TEXT NOT NULL,
        rationale TEXT NOT NULL,
        architectural_impact REAL DEFAULT 0.0,
        consequences TEXT
    );

    CREATE TABLE IF NOT EXISTS design_alternatives (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id TEXT NOT NULL,
        option_name TEXT NOT NULL,
        tradeoffs TEXT,
        rejected_reason TEXT,
        FOREIGN KEY(decision_id) REFERENCES decision_nodes(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS failed_experiments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        target_subsystem TEXT NOT NULL,
        patch TEXT NOT NULL,
        error_type TEXT NOT NULL,
        error_message TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS repo_personality_styles (
        subsystem TEXT PRIMARY KEY,
        naming_conventions TEXT NOT NULL,
        type_hinting_strictness REAL NOT NULL,
        preferred_constructs TEXT NOT NULL,
        class_vs_functional TEXT NOT NULL,
        docstring_style TEXT NOT NULL,
        updated_at REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS repository_evolution_timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp REAL NOT NULL,
        subsystem TEXT NOT NULL,
        lcom_average REAL NOT NULL,
        coupling_ratio REAL NOT NULL,
        debt_items_count INTEGER NOT NULL,
        major_milestone TEXT,
        rationales_summary TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_decisions_subsystem ON decision_nodes(target_subsystem);
    CREATE INDEX IF NOT EXISTS idx_failures_subsystem ON failed_experiments(target_subsystem);
    CREATE INDEX IF NOT EXISTS idx_personality_subsystem ON repo_personality_styles(subsystem);
    CREATE INDEX IF NOT EXISTS idx_evolution_subsystem ON repository_evolution_timeline(subsystem);
"""


class LineageMemoryTier:
    """Persistent storage tier for architectural decisions (DLS) and failed experiments (FEL).

    All reads and writes are async and serialised through an
    :class:`~velune.memory.storage.sqlite_pool.SQLiteConnectionPool` to
    prevent file-locking contention.
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
        logger.debug("LineageMemoryTier schema initialised.")

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def log_decision(
        self,
        decision_id: str,
        target_subsystem: str,
        rationale: str,
        architectural_impact: float = 0.0,
        consequences: str | None = None,
        alternatives: list[dict[str, Any]] | None = None,
    ) -> None:
        """Log an approved architectural decision and its design trade-offs."""
        decision_sql = """
            INSERT INTO decision_nodes
                (id, timestamp, target_subsystem, rationale, architectural_impact, consequences)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                target_subsystem=excluded.target_subsystem,
                rationale=excluded.rationale,
                architectural_impact=excluded.architectural_impact,
                consequences=excluded.consequences
        """
        params = (
            decision_id,
            time.time(),
            target_subsystem,
            rationale,
            architectural_impact,
            consequences or "",
        )
        try:
            async with self._pool.write() as conn:
                await conn.execute(decision_sql, params)
                if alternatives:
                    await conn.execute(
                        "DELETE FROM design_alternatives WHERE decision_id = ?",
                        (decision_id,),
                    )
                    for alt in alternatives:
                        await conn.execute(
                            """
                            INSERT INTO design_alternatives
                                (decision_id, option_name, tradeoffs, rejected_reason)
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                decision_id,
                                alt.get("option_name", "Option"),
                                json.dumps(alt.get("tradeoffs", {})),
                                alt.get("rejected_reason", ""),
                            ),
                        )
        except Exception as exc:
            logger.error("Checkpoint save failed: %s", exc)
            raise

    async def log_failed_experiment(
        self,
        target_subsystem: str,
        patch: str,
        error_type: str,
        error_message: str,
    ) -> None:
        """Log a failed implementation experiment (FEL) to prevent repeating it."""
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    """
                    INSERT INTO failed_experiments
                        (timestamp, target_subsystem, patch, error_type, error_message)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (time.time(), target_subsystem, patch, error_type, error_message),
                )
        except Exception as exc:
            logger.error("Failed to log experiment: %s", exc)

    async def save_personality_style(
        self,
        subsystem: str,
        naming_conventions: dict,
        type_hinting_strictness: float,
        preferred_constructs: list,
        class_vs_functional: str,
        docstring_style: str,
    ) -> None:
        """Persist a subsystem's repository personality and style profile."""
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    """
                    INSERT INTO repo_personality_styles (
                        subsystem, naming_conventions, type_hinting_strictness,
                        preferred_constructs, class_vs_functional, docstring_style,
                        updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(subsystem) DO UPDATE SET
                        naming_conventions=excluded.naming_conventions,
                        type_hinting_strictness=excluded.type_hinting_strictness,
                        preferred_constructs=excluded.preferred_constructs,
                        class_vs_functional=excluded.class_vs_functional,
                        docstring_style=excluded.docstring_style,
                        updated_at=excluded.updated_at
                    """,
                    (
                        subsystem,
                        json.dumps(naming_conventions),
                        type_hinting_strictness,
                        json.dumps(preferred_constructs),
                        class_vs_functional,
                        docstring_style,
                        time.time(),
                    ),
                )
        except Exception as exc:
            logger.error("Failed to save personality style: %s", exc)

    async def log_monthly_snapshot(
        self,
        subsystem: str,
        lcom_average: float,
        coupling_ratio: float,
        debt_items_count: int,
        milestone: str | None = None,
        rationale_summary: str = "",
    ) -> None:
        """Persist a monthly architecture snapshot for the given subsystem."""
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    """
                    INSERT INTO repository_evolution_timeline
                        (timestamp, subsystem, lcom_average, coupling_ratio,
                         debt_items_count, major_milestone, rationales_summary)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        time.time(),
                        subsystem,
                        round(lcom_average, 4),
                        round(coupling_ratio, 4),
                        int(debt_items_count),
                        milestone or "",
                        rationale_summary,
                    ),
                )
        except Exception as exc:
            logger.error("Failed to log monthly snapshot: %s", exc)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_subsystem_decisions(self, subsystem: str) -> list[dict[str, Any]]:
        """Fetch all decisions matching a target subsystem."""
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    """
                    SELECT id, timestamp, target_subsystem, rationale,
                           architectural_impact, consequences
                    FROM decision_nodes
                    WHERE target_subsystem LIKE ?
                    ORDER BY timestamp DESC
                    """,
                    (f"%{subsystem}%",),
                )
                rows = await cursor.fetchall()

            decisions = []
            for r in rows:
                dec = dict(r)
                async with self._pool.read() as conn2:
                    cur2 = await conn2.execute(
                        "SELECT option_name, tradeoffs, rejected_reason"
                        " FROM design_alternatives WHERE decision_id = ?",
                        (dec["id"],),
                    )
                    alt_rows = await cur2.fetchall()
                dec["alternatives"] = []
                for ar in alt_rows:
                    alt = dict(ar)
                    try:
                        alt["tradeoffs"] = json.loads(alt["tradeoffs"])
                    except Exception:
                        alt["tradeoffs"] = {}
                    dec["alternatives"].append(alt)
                decisions.append(dec)
            return decisions
        except Exception as exc:
            logger.error("Failed to query subsystem decisions: %s", exc)
            return []

    async def get_failed_experiments(self, subsystem: str) -> list[dict[str, Any]]:
        """Fetch all failed experiment records matching a target subsystem."""
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    """
                    SELECT id, timestamp, target_subsystem, patch,
                           error_type, error_message
                    FROM failed_experiments
                    WHERE target_subsystem LIKE ?
                    ORDER BY timestamp DESC
                    """,
                    (f"%{subsystem}%",),
                )
                rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("Failed to query failed experiments: %s", exc)
            return []

    async def get_personality_style(self, subsystem: str) -> dict[str, Any] | None:
        """Query the style profile for a subsystem."""
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    """
                    SELECT subsystem, naming_conventions, type_hinting_strictness,
                           preferred_constructs, class_vs_functional,
                           docstring_style, updated_at
                    FROM repo_personality_styles
                    WHERE subsystem = ?
                    """,
                    (subsystem,),
                )
                row = await cursor.fetchone()
            if row:
                res = dict(row)
                try:
                    res["naming_conventions"] = json.loads(res["naming_conventions"])
                except Exception:
                    res["naming_conventions"] = {}
                try:
                    res["preferred_constructs"] = json.loads(res["preferred_constructs"])
                except Exception:
                    res["preferred_constructs"] = []
                return res
            return None
        except Exception as exc:
            logger.error("Failed to query repository personality style: %s", exc)
            return None

    async def get_evolution_timeline(
        self, subsystem: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Fetch the evolution snapshots for a subsystem, most recent first."""
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    """
                    SELECT id, timestamp, subsystem, lcom_average, coupling_ratio,
                           debt_items_count, major_milestone, rationales_summary
                    FROM repository_evolution_timeline
                    WHERE subsystem LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (f"%{subsystem}%", limit),
                )
                rows = await cursor.fetchall()
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.error("Failed to query evolution timeline: %s", exc)
            return []

    async def query_continuity_warnings(
        self,
        prompt: str,
        repo_context: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Match task keywords against stored decisions and failed experiments."""
        combined_text = (prompt + " " + repo_context).lower()
        subsystem_keys = [
            "database", "db", "concurrency", "lock", "thread", "async", "cache",
            "sandbox", "security", "telemetry", "model", "routing", "memory", "graph",
            "watcher", "file", "lifecycle", "executor", "bus", "registry",
        ]

        matched_decisions: list[dict[str, Any]] = []
        matched_failures: list[dict[str, Any]] = []

        for key in subsystem_keys:
            if key in combined_text:
                matched_decisions.extend(await self.get_subsystem_decisions(key))
                matched_failures.extend(await self.get_failed_experiments(key))

        unique_decisions = {d["id"]: d for d in matched_decisions}.values()
        decisions_list = sorted(unique_decisions, key=lambda x: x["timestamp"], reverse=True)[:3]

        unique_failures = {f["id"]: f for f in matched_failures}.values()
        failures_list = sorted(unique_failures, key=lambda x: x["timestamp"], reverse=True)[:3]

        return list(decisions_list), list(failures_list)
