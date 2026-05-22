"""Import/dependency graph builder."""

from typing import Dict, list
from pathlib import Path
import networkx as nx
from velune.core.types import DependencyEdge


class DependencyGraphBuilder:
    """Builds dependency graphs from import relationships."""

    def __init__(self):
        self.graph = nx.DiGraph()

    def add_file(self, file_path: str) -> None:
        """Add a file node to the graph."""
        self.graph.add_node(file_path, type="file")

    def add_dependency(self, source: str, target: str, dep_type: str = "import") -> None:
        """Add a dependency edge to the graph."""
        self.graph.add_edge(source, target, type=dep_type)

    def get_dependencies(self, file_path: str) -> list[str]:
        """Get dependencies for a file."""
        if file_path not in self.graph:
            return []
        return list(self.graph.successors(file_path))

    def get_dependents(self, file_path: str) -> list[str]:
        """Get files that depend on this file."""
        if file_path not in self.graph:
            return []
        return list(self.graph.predecessors(file_path))

    def get_cycles(self) -> list[list[str]]:
        """Detect circular dependencies."""
        return list(nx.simple_cycles(self.graph))

    def to_edges(self) -> list[DependencyEdge]:
        """Convert graph to dependency edges."""
        edges = []
        for source, target, data in self.graph.edges(data=True):
            edges.append(
                DependencyEdge(
                    source=source,
                    target=target,
                    edge_type=data.get("type", "import"),
                )
            )
        return edges
