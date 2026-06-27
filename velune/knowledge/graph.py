"""Async SQLite-backed Repository Knowledge Graph store.

Persists code-structure nodes and edges to a dedicated SQLite database.
Intentionally separate from velune/memory/storage (cognitive state) and
velune/repository (raw analysis) — this layer owns the AI-queryable
semantic graph of the codebase.

Storage: ~/.velune/knowledge_graph.db  (WAL mode, single write-lock).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from velune.knowledge.schemas import (
    EdgeType,
    KnowledgeEdge,
    KnowledgeGraphStats,
    KnowledgeNode,
    NodeType,
)

logger = logging.getLogger("velune.knowledge.graph")

_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS kg_nodes (
        id          TEXT PRIMARY KEY,
        node_type   TEXT NOT NULL,
        label       TEXT NOT NULL,
        file_path   TEXT,
        line_start  INTEGER,
        line_end    INTEGER,
        metadata    TEXT NOT NULL DEFAULT '{}',
        updated_at  REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS kg_edges (
        source      TEXT NOT NULL,
        target      TEXT NOT NULL,
        edge_type   TEXT NOT NULL,
        weight      REAL NOT NULL DEFAULT 1.0,
        metadata    TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (source, target, edge_type),
        FOREIGN KEY (source) REFERENCES kg_nodes(id) ON DELETE CASCADE,
        FOREIGN KEY (target) REFERENCES kg_nodes(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS kg_meta (
        key         TEXT PRIMARY KEY,
        value       TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_kg_nodes_type     ON kg_nodes(node_type);
    CREATE INDEX IF NOT EXISTS idx_kg_nodes_file     ON kg_nodes(file_path);
    CREATE INDEX IF NOT EXISTS idx_kg_edges_source   ON kg_edges(source);
    CREATE INDEX IF NOT EXISTS idx_kg_edges_target   ON kg_edges(target);
    CREATE INDEX IF NOT EXISTS idx_kg_edges_type     ON kg_edges(edge_type);
"""

_WRITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
)


class KnowledgeGraph:
    """Async SQLite-backed store for the Repository Knowledge Graph.

    Lifecycle
    ---------
    Call ``await graph.initialize()`` before any reads or writes.
    Multiple coroutines may read concurrently; writes are serialised
    through a single asyncio.Lock.

    Usage
    -----
    Typical write session::

        await graph.upsert_node(node)
        await graph.upsert_edge(edge)

    Typical read session::

        node = await graph.get_node(node_id)
        neighbors = await graph.neighbors(node_id)
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Create tables and indexes if they do not exist yet."""
        async with self._write() as conn:
            for stmt in _SCHEMA_SQL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    await conn.execute(stmt)
        logger.debug("KnowledgeGraph schema initialized at %s", self._db_path)

    async def clear(self) -> None:
        """Delete all nodes and edges (preserves schema)."""
        async with self._write() as conn:
            await conn.execute("DELETE FROM kg_edges")
            await conn.execute("DELETE FROM kg_nodes")
            await conn.execute("DELETE FROM kg_meta")
        logger.debug("KnowledgeGraph cleared.")

    # ------------------------------------------------------------------
    # Internal connection helpers
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _write(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._write_lock:
            async with aiosqlite.connect(self._db_path) as conn:
                conn.row_factory = aiosqlite.Row
                for pragma in _WRITE_PRAGMAS:
                    await conn.execute(pragma)
                try:
                    yield conn
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise

    @asynccontextmanager
    async def _read(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self._db_path) as conn:
            conn.row_factory = aiosqlite.Row
            await conn.execute("PRAGMA journal_mode=WAL")
            await conn.execute("PRAGMA foreign_keys=ON")
            yield conn

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def upsert_node(self, node: KnowledgeNode) -> None:
        """Insert or update a node."""
        meta = json.dumps(node.metadata)
        async with self._write() as conn:
            await conn.execute(
                """
                INSERT INTO kg_nodes (id, node_type, label, file_path, line_start, line_end,
                                      metadata, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    node_type  = excluded.node_type,
                    label      = excluded.label,
                    file_path  = excluded.file_path,
                    line_start = excluded.line_start,
                    line_end   = excluded.line_end,
                    metadata   = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    node.id,
                    node.node_type.value,
                    node.label,
                    node.file_path,
                    node.line_start,
                    node.line_end,
                    meta,
                    time.time(),
                ),
            )

    async def upsert_nodes_bulk(self, nodes: list[KnowledgeNode]) -> None:
        """Insert or update many nodes in a single transaction."""
        if not nodes:
            return
        rows = [
            (
                n.id,
                n.node_type.value,
                n.label,
                n.file_path,
                n.line_start,
                n.line_end,
                json.dumps(n.metadata),
                time.time(),
            )
            for n in nodes
        ]
        async with self._write() as conn:
            await conn.executemany(
                """
                INSERT INTO kg_nodes (id, node_type, label, file_path, line_start, line_end,
                                      metadata, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    node_type  = excluded.node_type,
                    label      = excluded.label,
                    file_path  = excluded.file_path,
                    line_start = excluded.line_start,
                    line_end   = excluded.line_end,
                    metadata   = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                rows,
            )

    async def upsert_edge(self, edge: KnowledgeEdge) -> None:
        """Insert or update a directed edge."""
        meta = json.dumps(edge.metadata)
        async with self._write() as conn:
            await conn.execute(
                """
                INSERT INTO kg_edges (source, target, edge_type, weight, metadata)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, target, edge_type) DO UPDATE SET
                    weight   = excluded.weight,
                    metadata = excluded.metadata
                """,
                (edge.source, edge.target, edge.edge_type.value, edge.weight, meta),
            )

    async def upsert_edges_bulk(self, edges: list[KnowledgeEdge]) -> None:
        """Insert or update many edges in a single transaction."""
        if not edges:
            return
        rows = [
            (e.source, e.target, e.edge_type.value, e.weight, json.dumps(e.metadata)) for e in edges
        ]
        async with self._write() as conn:
            await conn.executemany(
                """
                INSERT INTO kg_edges (source, target, edge_type, weight, metadata)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source, target, edge_type) DO UPDATE SET
                    weight   = excluded.weight,
                    metadata = excluded.metadata
                """,
                rows,
            )

    async def set_meta(self, key: str, value: str) -> None:
        """Store a metadata key-value pair (e.g. root_path, built_at)."""
        async with self._write() as conn:
            await conn.execute(
                """
                INSERT INTO kg_meta (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_node(self, node_id: str) -> KnowledgeNode | None:
        """Fetch a node by its identifier."""
        async with self._read() as conn:
            cursor = await conn.execute("SELECT * FROM kg_nodes WHERE id = ?", (node_id,))
            row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_node(row)

    async def get_nodes_by_file(self, file_path: str) -> list[KnowledgeNode]:
        """Return all nodes whose file_path matches exactly."""
        async with self._read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM kg_nodes WHERE file_path = ? ORDER BY line_start",
                (file_path,),
            )
            rows = await cursor.fetchall()
        return [_row_to_node(r) for r in rows]

    async def get_nodes_by_type(self, node_type: NodeType) -> list[KnowledgeNode]:
        """Return all nodes of a given type."""
        async with self._read() as conn:
            cursor = await conn.execute(
                "SELECT * FROM kg_nodes WHERE node_type = ?", (node_type.value,)
            )
            rows = await cursor.fetchall()
        return [_row_to_node(r) for r in rows]

    async def neighbors(
        self,
        node_id: str,
        edge_type: EdgeType | None = None,
        direction: str = "out",
    ) -> list[tuple[KnowledgeNode, KnowledgeEdge]]:
        """Return adjacent nodes and the connecting edge.

        Parameters
        ----------
        node_id:
            The node to query.
        edge_type:
            If given, filter to only this edge type.
        direction:
            ``"out"`` follows outgoing edges (default),
            ``"in"`` follows incoming edges,
            ``"both"`` follows both directions.
        """
        results: list[tuple[KnowledgeNode, KnowledgeEdge]] = []

        async def _fetch(src_col: str, tgt_col: str) -> None:
            params: list[Any] = [node_id]
            sql = f"""
                SELECT e.source, e.target, e.edge_type, e.weight, e.metadata,
                       n.id as nid, n.node_type, n.label, n.file_path,
                       n.line_start, n.line_end, n.metadata as nmeta
                FROM kg_edges e
                JOIN kg_nodes n ON e.{tgt_col} = n.id
                WHERE e.{src_col} = ?
            """
            if edge_type is not None:
                sql += " AND e.edge_type = ?"
                params.append(edge_type.value)
            async with self._read() as conn:
                cursor = await conn.execute(sql, params)
                rows = await cursor.fetchall()
            for row in rows:
                node = KnowledgeNode(
                    id=row["nid"],
                    node_type=NodeType(row["node_type"]),
                    label=row["label"],
                    file_path=row["file_path"],
                    line_start=row["line_start"],
                    line_end=row["line_end"],
                    metadata=json.loads(row["nmeta"] or "{}"),
                )
                edge = KnowledgeEdge(
                    source=row["source"],
                    target=row["target"],
                    edge_type=EdgeType(row["edge_type"]),
                    weight=row["weight"],
                    metadata=json.loads(row["metadata"] or "{}"),
                )
                results.append((node, edge))

        if direction in ("out", "both"):
            await _fetch("source", "target")
        if direction in ("in", "both"):
            await _fetch("target", "source")

        return results

    async def subgraph(
        self,
        root_id: str,
        depth: int = 2,
        edge_types: list[EdgeType] | None = None,
    ) -> tuple[list[KnowledgeNode], list[KnowledgeEdge]]:
        """BFS outward from root_id up to depth hops, optionally filtered by edge type.

        Returns the collected nodes and edges as two lists.
        """
        visited_nodes: dict[str, KnowledgeNode] = {}
        visited_edges: list[KnowledgeEdge] = []
        queue = [(root_id, 0)]
        seen_pairs: set[tuple[str, str, str]] = set()

        while queue:
            nid, d = queue.pop(0)
            if d >= depth:
                continue

            for node, edge in await self.neighbors(nid, direction="both"):
                key = (edge.source, edge.target, edge.edge_type.value)
                if key in seen_pairs:
                    continue
                if edge_types and edge.edge_type not in edge_types:
                    continue
                seen_pairs.add(key)
                visited_edges.append(edge)
                if node.id not in visited_nodes:
                    visited_nodes[node.id] = node
                    queue.append((node.id, d + 1))

        # Always include the root node itself
        if root_id not in visited_nodes:
            root = await self.get_node(root_id)
            if root:
                visited_nodes[root_id] = root

        return list(visited_nodes.values()), visited_edges

    async def stats(self) -> KnowledgeGraphStats:
        """Return summary statistics about the current graph state."""
        async with self._read() as conn:
            nc_row = await (await conn.execute("SELECT COUNT(*) FROM kg_nodes")).fetchone()
            ec_row = await (await conn.execute("SELECT COUNT(*) FROM kg_edges")).fetchone()
            fc_row = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM kg_nodes WHERE node_type = ?", (NodeType.FILE.value,)
                )
            ).fetchone()
            sc_row = await (
                await conn.execute(
                    "SELECT COUNT(*) FROM kg_nodes WHERE node_type IN (?,?,?)",
                    (NodeType.CLASS.value, NodeType.FUNCTION.value, NodeType.METHOD.value),
                )
            ).fetchone()
            meta_row = await (
                await conn.execute("SELECT value FROM kg_meta WHERE key = 'root_path'")
            ).fetchone()
            built_row = await (
                await conn.execute("SELECT value FROM kg_meta WHERE key = 'built_at'")
            ).fetchone()

        return KnowledgeGraphStats(
            node_count=nc_row[0] if nc_row else 0,
            edge_count=ec_row[0] if ec_row else 0,
            file_count=fc_row[0] if fc_row else 0,
            symbol_count=sc_row[0] if sc_row else 0,
            root_path=meta_row["value"] if meta_row else "",
            built_at=float(built_row["value"]) if built_row else 0.0,
        )

    async def get_meta(self, key: str) -> str | None:
        """Read a metadata value by key."""
        async with self._read() as conn:
            row = await (
                await conn.execute("SELECT value FROM kg_meta WHERE key = ?", (key,))
            ).fetchone()
        return row["value"] if row else None

    async def delete_nodes_by_file(self, file_path: str) -> int:
        """Delete all nodes whose file_path matches exactly.

        ON DELETE CASCADE removes all edges referencing those nodes automatically.
        Returns the number of nodes deleted.
        """
        async with self._write() as conn:
            cursor = await conn.execute("DELETE FROM kg_nodes WHERE file_path = ?", (file_path,))
            return cursor.rowcount

    async def delete_nodes_by_files(self, file_paths: list[str]) -> int:
        """Bulk-delete nodes for multiple files in a single transaction."""
        if not file_paths:
            return 0
        async with self._write() as conn:
            await conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS temp_kg_delete_paths (path TEXT PRIMARY KEY)"
            )
            await conn.execute("DELETE FROM temp_kg_delete_paths")
            await conn.executemany(
                "INSERT OR IGNORE INTO temp_kg_delete_paths(path) VALUES (?)",
                ((path,) for path in file_paths),
            )
            cursor = await conn.execute(
                """
                DELETE FROM kg_nodes
                WHERE file_path IN (SELECT path FROM temp_kg_delete_paths)
                """
            )
            await conn.execute(
                """
                DELETE FROM kg_nodes
                WHERE id IN (SELECT 'file:' || path FROM temp_kg_delete_paths)
                """
            )
            return cursor.rowcount


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _row_to_node(row: aiosqlite.Row) -> KnowledgeNode:
    return KnowledgeNode(
        id=row["id"],
        node_type=NodeType(row["node_type"]),
        label=row["label"],
        file_path=row["file_path"],
        line_start=row["line_start"],
        line_end=row["line_end"],
        metadata=json.loads(row["metadata"] or "{}"),
    )
