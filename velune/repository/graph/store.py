"""Repository graph store (networkx + Graphiti)."""

import networkx as nx
from typing import Dict, list, Optional
from velune.repository.graph.dependency import DependencyGraphBuilder
from velune.repository.graph.call_graph import CallGraphBuilder


class RepositoryGraphStore:
    """Store for repository graphs."""

    def __init__(self):
        self.dependency_graph = DependencyGraphBuilder()
        self.call_graph = CallGraphBuilder()
        self.file_graph = nx.DiGraph()

    def add_file_node(self, file_path: str) -> None:
        """Add a file node."""
        self.file_graph.add_node(file_path)
        self.dependency_graph.add_file(file_path)

    def add_dependency(self, source: str, target: str) -> None:
        """Add a dependency."""
        self.dependency_graph.add_dependency(source, target)
        self.file_graph.add_edge(source, target)

    def add_function_node(self, function_id: str, file_path: str) -> None:
        """Add a function node."""
        self.call_graph.add_function(function_id, file_path)

    def add_call(self, caller: str, callee: str) -> None:
        """Add a function call."""
        self.call_graph.add_call(caller, callee)

    def get_file_dependencies(self, file_path: str) -> list[str]:
        """Get file dependencies."""
        return self.dependency_graph.get_dependencies(file_path)

    def get_function_calls(self, function_id: str) -> list[str]:
        """Get function calls."""
        return self.call_graph.get_calls(function_id)

    def get_file_graph(self) -> nx.DiGraph:
        """Get the file-level graph."""
        return self.file_graph
