"""LanceDB-backed vector store for semantic memory (Phase 2a).

LanceDB is an embedded, serverless vector database — no daemon process is
needed.  All heavy operations (open, add, search, delete) are pushed to a
thread pool via ``asyncio.to_thread`` so they never block the event loop.

Graceful degradation: if LanceDB or PyArrow is not installed, or if startup
fails, ``_degraded`` is set to True and all operations become safe no-ops.
Set ``VELUNE_SKIP_LANCEDB=1`` to force degraded mode for fast dev iteration.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("velune.memory.storage.lancedb_store")

# Default embedding dimension for nomic-embed-text via Ollama
EMBEDDING_DIM: int = 768

_TABLE_NAME = "memories"


# ── Data models ───────────────────────────────────────────────────────────────


@dataclass
class MemoryRecord:
    """A vector record to upsert into LanceDB."""

    id: str
    embedding: list[float]
    content: str
    source_type: str        # "turn_user", "turn_assistant", "session_summary"
    session_id: str
    turn_id: str
    workspace_root: str
    created_at: float
    trust_score: float = 1.0


@dataclass
class SearchResult:
    """A single result returned from a vector similarity search."""

    id: str
    content: str
    source_type: str
    distance: float
    trust_score: float
    session_id: str
    turn_id: str
    created_at: float = field(default_factory=time.time)


# ── Store ─────────────────────────────────────────────────────────────────────


class LanceDBStore:
    """Async wrapper around a local LanceDB table for semantic memory storage.

    Call ``await store.startup()`` (or ``await store.initialize()``) before
    any read/write operations; the module lifecycle does this automatically.
    """

    def __init__(
        self,
        store_path: Path,
        embedding_dim: int = EMBEDDING_DIM,
    ) -> None:
        self._store_path = store_path
        self._embedding_dim = embedding_dim
        self._db: Any = None
        self._table: Any = None
        self._degraded: bool = os.environ.get(
            "VELUNE_SKIP_LANCEDB", ""
        ).lower() in ("1", "true", "yes")
        if self._degraded:
            logger.warning("VELUNE_SKIP_LANCEDB set — LanceDB running in degraded mode.")

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Open (or create) the LanceDB store and the memories table."""
        if self._degraded:
            return
        self._store_path.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(self._open_or_create)
            logger.info("LanceDB store ready at %s (%d-dim)", self._store_path, self._embedding_dim)
        except Exception as exc:
            logger.error("LanceDB startup failed; falling back to degraded mode: %s", exc)
            self._degraded = True

    async def initialize(self) -> None:
        """Lifecycle alias called by the module coordinator."""
        await self.startup()

    async def shutdown(self) -> None:
        self._db = None
        self._table = None

    def _open_or_create(self) -> None:
        import lancedb
        import pyarrow as pa

        self._db = lancedb.connect(str(self._store_path))
        existing = self._db.table_names()

        if _TABLE_NAME in existing:
            self._table = self._db.open_table(_TABLE_NAME)
            logger.debug("Opened existing LanceDB table '%s'", _TABLE_NAME)
        else:
            schema = pa.schema([
                pa.field("id",             pa.utf8()),
                pa.field("embedding",      pa.list_(pa.float32(), self._embedding_dim)),
                pa.field("content",        pa.utf8()),
                pa.field("source_type",    pa.utf8()),
                pa.field("session_id",     pa.utf8()),
                pa.field("turn_id",        pa.utf8()),
                pa.field("workspace_root", pa.utf8()),
                pa.field("created_at",     pa.float64()),
                pa.field("trust_score",    pa.float32()),
            ])
            self._table = self._db.create_table(_TABLE_NAME, schema=schema)
            logger.info("Created LanceDB table '%s'", _TABLE_NAME)

    # ── Write operations ──────────────────────────────────────────────────────

    async def upsert(self, records: list[MemoryRecord]) -> None:
        """Delete any existing records with matching IDs, then insert fresh copies."""
        if self._degraded or not records or self._table is None:
            return
        try:
            await asyncio.to_thread(self._upsert_sync, records)
        except Exception as exc:
            logger.error("LanceDB upsert failed: %s", exc)

    def _upsert_sync(self, records: list[MemoryRecord]) -> None:
        ids = [r.id for r in records]
        ids_clause = ", ".join(f"'{_esc(i)}'" for i in ids)
        # Best-effort delete; ignore errors on an empty table
        try:
            self._table.delete(f"id IN ({ids_clause})")
        except Exception:
            pass

        rows = [
            {
                "id":             r.id,
                "embedding":      [float(x) for x in r.embedding],
                "content":        r.content,
                "source_type":    r.source_type,
                "session_id":     r.session_id,
                "turn_id":        r.turn_id,
                "workspace_root": r.workspace_root,
                "created_at":     float(r.created_at),
                "trust_score":    float(r.trust_score),
            }
            for r in records
        ]
        self._table.add(rows)

    async def delete(self, ids: list[str]) -> None:
        """Delete records by their string IDs."""
        if self._degraded or not ids or self._table is None:
            return
        try:
            await asyncio.to_thread(self._delete_sync, ids)
        except Exception as exc:
            logger.error("LanceDB delete failed: %s", exc)

    def _delete_sync(self, ids: list[str]) -> None:
        ids_clause = ", ".join(f"'{_esc(i)}'" for i in ids)
        self._table.delete(f"id IN ({ids_clause})")

    async def prune_by_trust(self, threshold: float) -> int:
        """Delete records whose trust_score falls below *threshold*.  Returns count deleted."""
        if self._degraded or self._table is None:
            return 0
        try:
            return await asyncio.to_thread(self._prune_sync, threshold)
        except Exception as exc:
            logger.error("LanceDB prune failed: %s", exc)
            return 0

    def _prune_sync(self, threshold: float) -> int:
        before = len(self._table)
        self._table.delete(f"trust_score < {threshold}")
        after = len(self._table)
        return max(0, before - after)

    # ── Search ─────────────────────────────────────────────────────────────────

    async def search(
        self,
        embedding: list[float],
        limit: int = 10,
        workspace_root: str | None = None,
    ) -> list[SearchResult]:
        """ANN search over the memories table, optionally pre-filtered by workspace."""
        if self._degraded or self._table is None:
            return []
        try:
            return await asyncio.to_thread(
                self._search_sync, embedding, limit, workspace_root
            )
        except Exception as exc:
            logger.error("LanceDB search failed: %s", exc)
            return []

    def _search_sync(
        self,
        embedding: list[float],
        limit: int,
        workspace_root: str | None,
    ) -> list[SearchResult]:
        import numpy as np

        query = np.array(embedding, dtype=np.float32)
        q = self._table.search(query, vector_column_name="embedding")

        if workspace_root:
            safe = _esc(workspace_root)
            q = q.where(f"workspace_root = '{safe}'", prefilter=True)

        rows = q.limit(limit).to_list()
        results: list[SearchResult] = []
        for row in rows:
            results.append(SearchResult(
                id=row["id"],
                content=row["content"],
                source_type=row["source_type"],
                distance=float(row.get("_distance", 0.0)),
                trust_score=float(row["trust_score"]),
                session_id=row["session_id"],
                turn_id=row["turn_id"],
                created_at=float(row.get("created_at", 0.0)),
            ))
        return results

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def table_size(self) -> int:
        if self._degraded or self._table is None:
            return 0
        try:
            return len(self._table)
        except Exception:
            return 0

    @property
    def is_healthy(self) -> bool:
        return not self._degraded and self._table is not None

    def health_check(self) -> dict:
        return {
            "healthy": self.is_healthy,
            "degraded": self._degraded,
            "store_path": str(self._store_path),
            "embedding_dim": self._embedding_dim,
            "table_size": self.table_size,
        }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    """Escape single quotes for safe SQL string literals in LanceDB WHERE clauses."""
    return s.replace("'", "''")
