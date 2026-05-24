"""Decision Lineage and Failed Experiment Memory Tier.

SQLite-backed persistent store with thread-safe queue writes
for long-running cognitive continuity and failed approach blocking.
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("velune.memory.tiers.lineage")


class LineageMemoryTier:
    """Persistent storage tier for architectural decisions (DLS) and failed experiments (FEL).

    Employs a single-threaded write queue to prevent SQLite file-locking contention
    under high concurrent developer or council operations.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.write_queue: queue.Queue[Tuple[str, tuple]] = queue.Queue()
        self.is_running = True
        
        # Ensure database directories exist
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

        # Start dedicated background worker for sequential writes
        self.write_thread = threading.Thread(target=self._process_write_queue, daemon=True)
        self.write_thread.start()

    def shutdown(self) -> None:
        """Gracefully terminate background write processing."""
        self.is_running = False
        # Push sentinel item to wake up queue wait
        self.write_queue.put(("", ()))
        self.write_thread.join(timeout=1.0)

    def _init_db(self) -> None:
        """Initialize SQLite tables for DLS decisions and FEL failed attempts."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Decision Nodes Table (DLS)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS decision_nodes (
                        id TEXT PRIMARY KEY,
                        timestamp REAL NOT NULL,
                        target_subsystem TEXT NOT NULL,
                        rationale TEXT NOT NULL,
                        architectural_impact REAL DEFAULT 0.0,
                        consequences TEXT
                    )
                """)
                
                # Design Alternatives (DLS tradeoffs)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS design_alternatives (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        decision_id TEXT NOT NULL,
                        option_name TEXT NOT NULL,
                        tradeoffs TEXT,
                        rejected_reason TEXT,
                        FOREIGN KEY(decision_id) REFERENCES decision_nodes(id) ON DELETE CASCADE
                    )
                """)
                
                # Failed Experiments (FEL)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS failed_experiments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        target_subsystem TEXT NOT NULL,
                        patch TEXT NOT NULL,
                        error_type TEXT NOT NULL,
                        error_message TEXT NOT NULL
                    )
                """)

                # Repository Personality & Styles (Phase 4)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS repo_personality_styles (
                        subsystem TEXT PRIMARY KEY,
                        naming_conventions TEXT NOT NULL,
                        type_hinting_strictness REAL NOT NULL,
                        preferred_constructs TEXT NOT NULL,
                        class_vs_functional TEXT NOT NULL,
                        docstring_style TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )
                """)

                # Monthly Subsystem Evolution Timeline (Phase 5)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS repository_evolution_timeline (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        subsystem TEXT NOT NULL,
                        lcom_average REAL NOT NULL,
                        coupling_ratio REAL NOT NULL,
                        debt_items_count INTEGER NOT NULL,
                        major_milestone TEXT,
                        rationales_summary TEXT NOT NULL
                    )
                """)

                # Performance Indices
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_decisions_subsystem ON decision_nodes(target_subsystem)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_failures_subsystem ON failed_experiments(target_subsystem)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_personality_subsystem ON repo_personality_styles(subsystem)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_evolution_subsystem ON repository_evolution_timeline(subsystem)")
                
                conn.commit()
            logger.info("Lineage database successfully initialized at %s", self.db_path)
        except Exception as e:
            logger.error("Failed to initialize lineage database: %s", e)

    def _process_write_queue(self) -> None:
        """Background worker thread processing all SQLite writes sequentially."""
        while self.is_running:
            try:
                # Wait for database write request
                query, params = self.write_queue.get(timeout=0.5)
                if not query:
                    # Thread shutdown sentinel
                    self.write_queue.task_done()
                    continue

                self._execute_write(query, params)
                self.write_queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error("Error in database write queue loop: %s", e)

    def _execute_write(self, query: str, params: tuple) -> None:
        """Synchronously execute a write operation in a dedicated thread connection."""
        max_retries = 3
        retry_delay = 0.1
        
        for attempt in range(max_retries):
            try:
                with sqlite3.connect(self.db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(query, params)
                    conn.commit()
                return  # Success
            except sqlite3.OperationalError as oe:
                if "locked" in str(oe).lower() and attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                logger.error("Database write execution failed on retry %d: %s", attempt, oe)
                break
            except Exception as e:
                logger.error("Database write execution failed: %s", e)
                break

    # =====================================================================
    # Writer Interfaces (Enqueued Asynchronously)
    # =====================================================================

    def log_decision(
        self,
        decision_id: str,
        target_subsystem: str,
        rationale: str,
        architectural_impact: float = 0.0,
        consequences: Optional[str] = None,
        alternatives: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Log an approved architectural decision and design trade-offs."""
        # Queue the decision insert
        decision_query = """
            INSERT INTO decision_nodes (id, timestamp, target_subsystem, rationale, architectural_impact, consequences)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                target_subsystem=excluded.target_subsystem,
                rationale=excluded.rationale,
                architectural_impact=excluded.architectural_impact,
                consequences=excluded.consequences
        """
        params = (decision_id, time.time(), target_subsystem, rationale, architectural_impact, consequences or "")
        self.write_queue.put((decision_query, params))

        # Queue any design alternative tradeoffs
        if alternatives:
            # First clear any stale options for this decision ID
            clear_query = "DELETE FROM design_alternatives WHERE decision_id = ?"
            self.write_queue.put((clear_query, (decision_id,)))
            
            for alt in alternatives:
                alt_query = """
                    INSERT INTO design_alternatives (decision_id, option_name, tradeoffs, rejected_reason)
                    VALUES (?, ?, ?, ?)
                """
                alt_params = (
                    decision_id,
                    alt.get("option_name", "Option"),
                    json.dumps(alt.get("tradeoffs", {})),
                    alt.get("rejected_reason", ""),
                )
                self.write_queue.put((alt_query, alt_params))

    def log_failed_experiment(
        self,
        target_subsystem: str,
        patch: str,
        error_type: str,
        error_message: str,
    ) -> None:
        """Log a failed implementation experiment (FEL) to prevent repeating it."""
        query = """
            INSERT INTO failed_experiments (timestamp, target_subsystem, patch, error_type, error_message)
            VALUES (?, ?, ?, ?, ?)
        """
        params = (time.time(), target_subsystem, patch, error_type, error_message)
        self.write_queue.put((query, params))

    # =====================================================================
    # Query Interfaces (Executed Synchronously / Thread-safe Reads)
    # =====================================================================

    def get_subsystem_decisions(self, subsystem: str) -> List[Dict[str, Any]]:
        """Fetch all decisions matching a target subsystem."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, timestamp, target_subsystem, rationale, architectural_impact, consequences
                    FROM decision_nodes
                    WHERE target_subsystem LIKE ?
                    ORDER BY timestamp DESC
                    """,
                    (f"%{subsystem}%",),
                )
                rows = cursor.fetchall()
                decisions = []
                for r in rows:
                    dec = dict(r)
                    # Fetch alternatives
                    cursor.execute(
                        "SELECT option_name, tradeoffs, rejected_reason FROM design_alternatives WHERE decision_id = ?",
                        (dec["id"],),
                    )
                    alt_rows = cursor.fetchall()
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
        except Exception as e:
            logger.error("Failed to query subsystem decisions: %s", e)
            return []

    def get_failed_experiments(self, subsystem: str) -> List[Dict[str, Any]]:
        """Fetch all failed experiment records matching a target subsystem."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT id, timestamp, target_subsystem, patch, error_type, error_message
                    FROM failed_experiments
                    WHERE target_subsystem LIKE ?
                    ORDER BY timestamp DESC
                    """,
                    (f"%{subsystem}%",),
                )
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to query failed experiments: %s", e)
            return []

    def query_continuity_warnings(self, prompt: str, repo_context: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Examine task details to extract and match architectural intent warnings and failed attempts.

        Fuzzy keyword matching scans task tags against database indices.
        """
        combined_text = (prompt + " " + repo_context).lower()
        
        # Dynamic keywords indicating subsystems
        subsystem_keys = [
            "database", "db", "concurrency", "lock", "thread", "async", "cache", 
            "sandbox", "security", "telemetry", "model", "routing", "memory", "graph", 
            "watcher", "file", "lifecycle", "executor", "bus", "registry"
        ]
        
        matched_decisions = []
        matched_failures = []
        
        for key in subsystem_keys:
            if key in combined_text:
                # Query matches
                matched_decisions.extend(self.get_subsystem_decisions(key))
                matched_failures.extend(self.get_failed_experiments(key))
                
        # Remove duplicates
        unique_decisions = {d["id"]: d for d in matched_decisions}.values()
        # Sort and limit
        decisions_list = sorted(unique_decisions, key=lambda x: x["timestamp"], reverse=True)[:3]
        
        # Deduplicate failures by ID
        unique_failures = {f["id"]: f for f in matched_failures}.values()
        failures_list = sorted(unique_failures, key=lambda x: x["timestamp"], reverse=True)[:3]
        
        return list(decisions_list), list(failures_list)

    def save_personality_style(
        self,
        subsystem: str,
        naming_conventions: dict,
        type_hinting_strictness: float,
        preferred_constructs: list,
        class_vs_functional: str,
        docstring_style: str,
    ) -> None:
        """Asynchronously save a subsystem's repository personality and style profile in SQLite."""
        query = """
            INSERT INTO repo_personality_styles (
                subsystem, naming_conventions, type_hinting_strictness, 
                preferred_constructs, class_vs_functional, docstring_style, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(subsystem) DO UPDATE SET
                naming_conventions=excluded.naming_conventions,
                type_hinting_strictness=excluded.type_hinting_strictness,
                preferred_constructs=excluded.preferred_constructs,
                class_vs_functional=excluded.class_vs_functional,
                docstring_style=excluded.docstring_style,
                updated_at=excluded.updated_at
        """
        params = (
            subsystem,
            json.dumps(naming_conventions),
            type_hinting_strictness,
            json.dumps(preferred_constructs),
            class_vs_functional,
            docstring_style,
            time.time(),
        )
        self.write_queue.put((query, params))

    def get_personality_style(self, subsystem: str) -> Optional[Dict[str, Any]]:
        """Synchronously query the style profile for a subsystem."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT subsystem, naming_conventions, type_hinting_strictness, 
                           preferred_constructs, class_vs_functional, docstring_style, updated_at
                    FROM repo_personality_styles
                    WHERE subsystem = ?
                    """,
                    (subsystem,),
                )
                row = cursor.fetchone()
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
        except Exception as e:
            logger.error("Failed to query repository personality style: %s", e)
            return None

    # =====================================================================
    # Evolution Timeline Interfaces (Phase 5)
    # =====================================================================

    def log_monthly_snapshot(
        self,
        subsystem: str,
        lcom_average: float,
        coupling_ratio: float,
        debt_items_count: int,
        milestone: Optional[str] = None,
        rationale_summary: str = "",
    ) -> None:
        """Queue a monthly architecture snapshot for the given subsystem."""
        query = """
            INSERT INTO repository_evolution_timeline
                (timestamp, subsystem, lcom_average, coupling_ratio,
                 debt_items_count, major_milestone, rationales_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        params = (
            time.time(),
            subsystem,
            round(lcom_average, 4),
            round(coupling_ratio, 4),
            int(debt_items_count),
            milestone or "",
            rationale_summary,
        )
        self.write_queue.put((query, params))

    def get_evolution_timeline(
        self, subsystem: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Fetch the evolution snapshots for a subsystem, ordered by most-recent first."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
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
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.error("Failed to query evolution timeline: %s", e)
            return []
