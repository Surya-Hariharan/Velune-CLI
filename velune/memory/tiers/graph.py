"""Graph Memory Tier (Tier 4).

Lightweight SQLite-backed Knowledge Graph store for indexing entities
(files, functions, concepts) and their semantic edge relationships.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel, Field

logger = logging.getLogger("velune.memory.tiers.graph")


class GraphNode(BaseModel):
    """A single entity node in the Knowledge Graph."""
    id: str
    node_type: str  # e.g., 'file', 'symbol', 'concept', 'author'
    properties: Dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """A directed edge connecting two entities in the Knowledge Graph."""
    source: str
    target: str
    relation_type: str  # e.g., 'depends_on', 'calls', 'authored_by'
    properties: Dict[str, Any] = Field(default_factory=dict)


class GraphMemoryTier:
    """Tier 4: Structured entity-relationship store representing codebase and cognitive dependencies."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database tables for nodes and edges."""
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Nodes Table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS graph_nodes (
                        id TEXT PRIMARY KEY,
                        node_type TEXT NOT NULL,
                        properties TEXT NOT NULL
                    )
                """)
                
                # Edges Table
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS graph_edges (
                        source TEXT NOT NULL,
                        target TEXT NOT NULL,
                        relation_type TEXT NOT NULL,
                        properties TEXT NOT NULL,
                        PRIMARY KEY (source, target, relation_type),
                        FOREIGN KEY (source) REFERENCES graph_nodes(id) ON DELETE CASCADE,
                        FOREIGN KEY (target) REFERENCES graph_nodes(id) ON DELETE CASCADE
                    )
                """)
                
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON graph_edges(source)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON graph_edges(target)")
                
                conn.commit()
            logger.info("Successfully initialized Graph Database at %s", self.db_path)
        except Exception as e:
            logger.error("Failed to initialize Graph Database: %s", e)

    def add_node(self, node_id: str, node_type: str, properties: Optional[Dict[str, Any]] = None) -> None:
        """Insert or update a node in the graph."""
        props_str = json.dumps(properties or {})
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO graph_nodes (id, node_type, properties)
                    VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        node_type=excluded.node_type,
                        properties=excluded.properties
                    """,
                    (node_id, node_type, props_str)
                )
                conn.commit()
        except Exception as e:
            logger.error("Failed to add node %s: %s", node_id, e)

    def add_edge(self, source_id: str, target_id: str, relation_type: str, properties: Optional[Dict[str, Any]] = None) -> None:
        """Create a directed edge between two existing nodes."""
        props_str = json.dumps(properties or {})
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO graph_edges (source, target, relation_type, properties)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(source, target, relation_type) DO UPDATE SET
                        properties=excluded.properties
                    """,
                    (source_id, target_id, relation_type, props_str)
                )
                conn.commit()
        except Exception as e:
            logger.error("Failed to add edge from %s to %s: %s", source_id, target_id, e)

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        """Fetch a specific node by its identifier."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT id, node_type, properties FROM graph_nodes WHERE id = ?", (node_id,))
                row = cursor.fetchone()
                if row:
                    return GraphNode(
                        id=row["id"],
                        node_type=row["node_type"],
                        properties=json.loads(row["properties"]),
                    )
        except Exception as e:
            logger.error("Failed to query node %s: %s", node_id, e)
        return None

    def get_neighbors(self, node_id: str) -> List[Tuple[GraphNode, str, GraphEdge]]:
        """Find all neighboring nodes and their edge relations."""
        neighbors: List[Tuple[GraphNode, str, GraphEdge]] = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Query outgoing connections
                cursor.execute(
                    """
                    SELECT e.relation_type, e.properties as edge_props, n.id, n.node_type, n.properties as node_props
                    FROM graph_edges e
                    JOIN graph_nodes n ON e.target = n.id
                    WHERE e.source = ?
                    """,
                    (node_id,)
                )
                
                for row in cursor.fetchall():
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
        except Exception as e:
            logger.error("Failed to query graph neighbors for %s: %s", node_id, e)
        return neighbors

    def find_shortest_path(self, start_id: str, end_id: str, max_depth: int = 4) -> Optional[List[str]]:
        """BFS search to identify the shortest relationship path between two concepts."""
        if start_id == end_id:
            return [start_id]
            
        queue: List[List[str]] = [[start_id]]
        visited = {start_id}
        
        while queue:
            path = queue.pop(0)
            node = path[-1]
            
            if len(path) > max_depth:
                continue
                
            neighbors = self.get_neighbors(node)
            for neighbor_node, direction, _ in neighbors:
                n_id = neighbor_node.id
                if n_id == end_id:
                    return path + [end_id]
                if n_id not in visited:
                    visited.add(n_id)
                    queue.append(path + [n_id])
                    
        return None

    def upsert_entity(self, entity_id: str, entity_type: str, **properties: Any) -> None:
        """Upsert a node (entity) in the knowledge graph."""
        self.add_node(node_id=entity_id, node_type=entity_type, properties=properties)

    def upsert_relationship(self, source_id: str, target_id: str, relation_type: str, **properties: Any) -> None:
        """Upsert a directed edge (relationship) between two existing nodes."""
        self.add_edge(source_id=source_id, target_id=target_id, relation_type=relation_type, properties=properties)

