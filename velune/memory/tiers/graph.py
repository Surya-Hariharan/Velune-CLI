"""Graph Memory Tier (Tier 4).

Lightweight SQLite-backed Knowledge Graph store for indexing entities
(files, functions, concepts) and their semantic edge relationships.

All I/O is async and routed through
:class:`~velune.memory.storage.sqlite_pool.SQLiteConnectionPool`.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from velune.memory.storage.sqlite_pool import SQLiteConnectionPool

logger = logging.getLogger("velune.memory.tiers.graph")

_SCHEMA_SQL = """
    CREATE TABLE IF NOT EXISTS graph_nodes (
        id TEXT PRIMARY KEY,
        node_type TEXT NOT NULL,
        properties TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS graph_edges (
        source TEXT NOT NULL,
        target TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        properties TEXT NOT NULL,
        PRIMARY KEY (source, target, relation_type),
        FOREIGN KEY (source) REFERENCES graph_nodes(id) ON DELETE CASCADE,
        FOREIGN KEY (target) REFERENCES graph_nodes(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS execution_nodes (
        id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        node_type TEXT NOT NULL,
        status TEXT NOT NULL,
        parameters TEXT NOT NULL,
        outcome TEXT,
        timestamp REAL NOT NULL
    );

    CREATE TABLE IF NOT EXISTS execution_edges (
        source TEXT NOT NULL,
        target TEXT NOT NULL,
        relation_type TEXT NOT NULL,
        PRIMARY KEY (source, target, relation_type),
        FOREIGN KEY (source) REFERENCES execution_nodes(id) ON DELETE CASCADE,
        FOREIGN KEY (target) REFERENCES execution_nodes(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_edges_source ON graph_edges(source);
    CREATE INDEX IF NOT EXISTS idx_edges_target ON graph_edges(target);
    CREATE INDEX IF NOT EXISTS idx_exec_nodes_task ON execution_nodes(task_id);
"""


class GraphNode(BaseModel):
    """A single entity node in the Knowledge Graph."""

    id: str
    node_type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """A directed edge connecting two entities in the Knowledge Graph."""

    source: str
    target: str
    relation_type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class GraphMemoryTier:
    """Tier 4: Structured entity-relationship store for codebase and cognitive dependencies."""

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
        logger.debug("GraphMemoryTier schema initialised.")

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def add_node(
        self,
        node_id: str,
        node_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Insert or upsert a node."""
        props_str = json.dumps(properties or {})
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    """
                    INSERT INTO graph_nodes (id, node_type, properties)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        node_type=excluded.node_type,
                        properties=excluded.properties
                    """,
                    (node_id, node_type, props_str),
                )
        except Exception as exc:
            logger.error("Failed to add node %s: %s", node_id, exc)

    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Create or update a directed edge."""
        props_str = json.dumps(properties or {})
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    """
                    INSERT INTO graph_edges (source, target, relation_type, properties)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source, target, relation_type) DO UPDATE SET
                        properties=excluded.properties
                    """,
                    (source_id, target_id, relation_type, props_str),
                )
        except Exception as exc:
            logger.error("Failed to add edge %s→%s: %s", source_id, target_id, exc)

    async def upsert_entity(self, entity_id: str, entity_type: str, **properties: Any) -> None:
        """Upsert a node (entity) in the knowledge graph."""
        await self.add_node(node_id=entity_id, node_type=entity_type, properties=properties)

    async def upsert_relationship(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
        **properties: Any,
    ) -> None:
        """Upsert a directed edge (relationship) between two existing nodes."""
        await self.add_edge(
            source_id=source_id,
            target_id=target_id,
            relation_type=relation_type,
            properties=properties,
        )

    async def record_execution_node(
        self,
        node_id: str,
        task_id: str,
        node_type: str,
        status: str,
        parameters: dict[str, Any],
        outcome: str | None = None,
    ) -> None:
        """Record a step in the execution lineage graph."""
        params_str = json.dumps(parameters)
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    """
                    INSERT INTO execution_nodes
                        (id, task_id, node_type, status, parameters, outcome, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        status=excluded.status,
                        parameters=excluded.parameters,
                        outcome=excluded.outcome,
                        timestamp=excluded.timestamp
                    """,
                    (node_id, task_id, node_type, status, params_str, outcome, time.time()),
                )
        except Exception as exc:
            logger.error("Failed to record execution node %s: %s", node_id, exc)

    async def record_execution_edge(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
    ) -> None:
        """Record a transition edge between execution steps."""
        try:
            async with self._pool.write() as conn:
                await conn.execute(
                    """
                    INSERT INTO execution_edges (source, target, relation_type)
                    VALUES (?, ?, ?)
                    ON CONFLICT(source, target, relation_type) DO NOTHING
                    """,
                    (source_id, target_id, relation_type),
                )
        except Exception as exc:
            logger.error("Failed to record execution edge %s→%s: %s", source_id, target_id, exc)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_all_nodes(self) -> list[GraphNode]:
        """Retrieve all nodes from the knowledge graph."""
        nodes = []
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute("SELECT id, node_type, properties FROM graph_nodes")
                rows = await cursor.fetchall()
                for row in rows:
                    nodes.append(
                        GraphNode(
                            id=row["id"],
                            node_type=row["node_type"],
                            properties=json.loads(row["properties"]),
                        )
                    )
        except Exception as exc:
            logger.error("Failed to retrieve all graph nodes: %s", exc)
        return nodes

    async def get_all_edges(self) -> list[GraphEdge]:
        """Retrieve all edges from the knowledge graph."""
        edges = []
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT source, target, relation_type, properties FROM graph_edges"
                )
                rows = await cursor.fetchall()
                for row in rows:
                    edges.append(
                        GraphEdge(
                            source=row["source"],
                            target=row["target"],
                            relation_type=row["relation_type"],
                            properties=json.loads(row["properties"]),
                        )
                    )
        except Exception as exc:
            logger.error("Failed to retrieve all graph edges: %s", exc)
        return edges

    async def get_node(self, node_id: str) -> GraphNode | None:
        """Fetch a specific node by its identifier."""
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    "SELECT id, node_type, properties FROM graph_nodes WHERE id = ?",
                    (node_id,),
                )
                row = await cursor.fetchone()
            if row:
                return GraphNode(
                    id=row["id"],
                    node_type=row["node_type"],
                    properties=json.loads(row["properties"]),
                )
        except Exception as exc:
            logger.error("Failed to query node %s: %s", node_id, exc)
        return None

    async def get_neighbors(self, node_id: str) -> list[tuple[GraphNode, str, GraphEdge]]:
        """Find all neighbouring nodes and their edge relations."""
        neighbors: list[tuple[GraphNode, str, GraphEdge]] = []
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    """
                    SELECT e.relation_type, e.properties as edge_props,
                           n.id, n.node_type, n.properties as node_props
                    FROM graph_edges e
                    JOIN graph_nodes n ON e.target = n.id
                    WHERE e.source = ?
                    """,
                    (node_id,),
                )
                rows = await cursor.fetchall()
            for row in rows:
                target_node = GraphNode(
                    id=row["id"],
                    node_type=row["node_type"],
                    properties=json.loads(row["node_props"]),
                )
                edge = GraphEdge(
                    source=node_id,
                    target=row["id"],
                    relation_type=row["relation_type"],
                    properties=json.loads(row["edge_props"]),
                )
                neighbors.append((target_node, "outgoing", edge))
        except Exception as exc:
            logger.error("Failed to query graph neighbours for %s: %s", node_id, exc)
        return neighbors

    async def find_shortest_path(
        self,
        start_id: str,
        end_id: str,
        max_depth: int = 4,
    ) -> list[str] | None:
        """BFS to find the shortest relationship path between two nodes."""
        if start_id == end_id:
            return [start_id]

        queue: list[list[str]] = [[start_id]]
        visited = {start_id}

        while queue:
            path = queue.pop(0)
            node = path[-1]

            if len(path) > max_depth:
                continue

            for neighbor_node, _, _ in await self.get_neighbors(node):
                n_id = neighbor_node.id
                if n_id == end_id:
                    return path + [end_id]
                if n_id not in visited:
                    visited.add(n_id)
                    queue.append(path + [n_id])

        return None

    async def query_execution_lineage(self, file_path: str) -> list[dict[str, Any]]:
        """Query historical execution lineage for a file path."""
        attempts = []
        try:
            async with self._pool.read() as conn:
                cursor = await conn.execute(
                    """
                    SELECT id, task_id, node_type, status, parameters, outcome, timestamp
                    FROM execution_nodes
                    WHERE parameters LIKE ?
                    ORDER BY timestamp DESC
                    """,
                    (f"%{file_path}%",),
                )
                rows = await cursor.fetchall()
            for row in rows:
                attempts.append(
                    {
                        "id": row["id"],
                        "task_id": row["task_id"],
                        "node_type": row["node_type"],
                        "status": row["status"],
                        "parameters": json.loads(row["parameters"]),
                        "outcome": row["outcome"],
                        "timestamp": row["timestamp"],
                    }
                )
        except Exception as exc:
            logger.error("Failed to query execution lineage for %s: %s", file_path, exc)
        return attempts
