"""Graph-based memory service."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional

import networkx as nx


class GraphMemoryService:
    """A replaceable graph memory built on top of networkx."""

    def __init__(self) -> None:
        self.graph = nx.MultiDiGraph()

    def upsert_entity(self, entity_id: str, entity_type: str, **properties: Any) -> None:
        self.graph.add_node(entity_id, kind=entity_type, **properties)

    def upsert_relationship(self, source_id: str, target_id: str, relationship_type: str, weight: float = 1.0, **properties: Any) -> None:
        self.graph.add_edge(source_id, target_id, relationship_type=relationship_type, weight=weight, **properties)

    def get_entity(self, entity_id: str) -> Optional[dict[str, Any]]:
        return dict(self.graph.nodes[entity_id]) if entity_id in self.graph else None

    def neighbors(self, entity_id: str, depth: int = 1) -> list[str]:
        if entity_id not in self.graph:
            return []
        seen = {entity_id}
        frontier = {entity_id}
        for _ in range(depth):
            next_frontier: set[str] = set()
            for current in frontier:
                next_frontier.update(self.graph.successors(current))
                next_frontier.update(self.graph.predecessors(current))
            frontier = next_frontier - seen
            seen.update(frontier)
        return list(seen)

    def relationships(self, entity_id: str) -> list[dict[str, Any]]:
        if entity_id not in self.graph:
            return []
        results: list[dict[str, Any]] = []
        for source, target, data in self.graph.edges(entity_id, data=True):
            results.append({"source": source, "target": target, **data})
        for source, target, data in self.graph.in_edges(entity_id, data=True):
            results.append({"source": source, "target": target, **data})
        return results

    def summary(self) -> dict[str, Any]:
        return {
            "nodes": self.graph.number_of_nodes(),
            "edges": self.graph.number_of_edges(),
            "node_types": sorted({data.get("kind", "unknown") for _, data in self.graph.nodes(data=True)}),
        }