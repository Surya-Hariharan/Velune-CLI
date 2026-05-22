"""Function call graph builder."""

from typing import Dict, list
import networkx as nx
from velune.core.types import SymbolNode


class CallGraphBuilder:
    """Builds function call graphs."""

    def __init__(self):
        self.graph = nx.DiGraph()

    def add_function(self, function_id: str, file_path: str) -> None:
        """Add a function node to the graph."""
        self.graph.add_node(function_id, file=file_path, type="function")

    def add_call(self, caller: str, callee: str) -> None:
        """Add a call edge to the graph."""
        self.graph.add_edge(caller, callee, type="call")

    def get_calls(self, function_id: str) -> list[str]:
        """Get functions called by this function."""
        if function_id not in self.graph:
            return []
        return list(self.graph.successors(function_id))

    def get_callers(self, function_id: str) -> list[str]:
        """Get functions that call this function."""
        if function_id not in self.graph:
            return []
        return list(self.graph.predecessors(function_id))

    def get_call_chain(self, from_func: str, to_func: str) -> list[str]:
        """Get call chain between two functions."""
        try:
            return nx.shortest_path(self.graph, from_func, to_func)
        except nx.NetworkXNoPath:
            return []
