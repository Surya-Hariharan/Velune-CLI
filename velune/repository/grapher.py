"""Dependency and import grapher using networkx."""

import os
from pathlib import Path

import networkx as nx

from velune.repository.schemas import RepositoryEdge, RepositorySymbol, RepositorySymbolKind


class RepositoryGrapher:
    """Builds and analyzes dependency graphs for repository files and symbols."""

    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path.resolve()
        self.graph = nx.MultiDiGraph()

    def add_file(self, file_path: str, language: str, size_bytes: int) -> None:
        """Adds a file node to the dependency graph."""
        # Normalize paths relative to workspace
        rel_path = self._to_rel_path(file_path)
        self.graph.add_node(
            rel_path,
            kind="file",
            language=language,
            size_bytes=size_bytes,
            type="file"
        )

    def add_symbol(self, symbol: RepositorySymbol) -> None:
        """Adds a symbol node and binds it to its containing file."""
        file_rel = self._to_rel_path(symbol.file_path)
        sym_id = symbol.symbol_id or symbol.name

        # Add symbol node
        self.graph.add_node(
            sym_id,
            name=symbol.name,
            qualified_name=symbol.qualified_name or symbol.name,
            kind=symbol.kind.value,
            file_path=file_rel,
            line_start=symbol.line_start,
            line_end=symbol.line_end,
            type="symbol"
        )

        # Draw containing relationship
        self.graph.add_edge(file_rel, sym_id, edge_type="contains", weight=1.0)

    def add_edge(self, edge: RepositoryEdge) -> None:
        """Adds a relationship edge between files or symbols."""
        source_rel = self._to_rel_path(edge.source)
        target_rel = self._to_rel_path(edge.target)
        self.graph.add_edge(
            source_rel,
            target_rel,
            edge_type=edge.edge_type,
            weight=edge.weight
        )

    def resolve_import_dependencies(self, files: list[str], symbols: list[RepositorySymbol]) -> None:
        """Resolves module imports to concrete files and draws file-to-file import edges."""
        # Map module names and symbol names to files
        file_by_module: dict[str, str] = {}
        for f in files:
            rel = self._to_rel_path(f)
            # e.g., velune/kernel/bus.py -> velune.kernel.bus
            mod_name = rel.replace(".py", "").replace("/", ".").replace("\\", ".")
            file_by_module[mod_name] = rel

            # Keep index.js -> index, index.ts -> index conversions
            if rel.endswith(("__init__.py", "index.ts", "index.js")):
                parent_mod = os.path.dirname(rel).replace("/", ".").replace("\\", ".")
                file_by_module[parent_mod] = rel

        # Map import symbols to files
        for sym in symbols:
            if sym.kind == RepositorySymbolKind.IMPORT:
                source_file = self._to_rel_path(sym.file_path)
                import_name = sym.name

                # Check direct module name match
                # e.g. from velune.kernel.bus import CognitiveBus -> import_name is "velune.kernel.bus"
                matched_file = file_by_module.get(import_name)

                # Fallback: check metadata module
                if not matched_file and "module" in sym.metadata:
                    mod = sym.metadata["module"]
                    matched_file = file_by_module.get(mod)

                # If still not found, check relative import resolution
                if not matched_file and import_name.startswith("."):
                    source_dir = os.path.dirname(source_file)
                    # Resolve relative dot hierarchy
                    dots = len(import_name) - len(import_name.lstrip("."))
                    parts = source_dir.split(os.sep) if source_dir else []
                    if len(parts) >= dots - 1:
                        target_dir_parts = parts[:len(parts) - (dots - 1)]
                        sub_mod = import_name.lstrip(".")
                        target_mod = ".".join(target_dir_parts + [sub_mod]) if target_dir_parts else sub_mod
                        matched_file = file_by_module.get(target_mod)

                if matched_file and source_file != matched_file:
                    self.graph.add_edge(source_file, matched_file, edge_type="imports", weight=1.0)

    def traverse(self, node_id: str, depth: int = 2) -> list[str]:
        """BFS traversal to discover connected file and symbol nodes up to specified depth."""
        node_rel = self._to_rel_path(node_id)
        
        # Determine starting nodes
        start_nodes = []
        if node_rel in self.graph:
            start_nodes.append(node_rel)
        elif node_id in self.graph:
            start_nodes.append(node_id)
        else:
            # Search by name or qualified_name in node attributes
            for n, data in self.graph.nodes(data=True):
                if data.get("type") == "symbol":
                    if data.get("name") == node_id or data.get("qualified_name") == node_id:
                        start_nodes.append(n)
        
        if not start_nodes:
            return []

        visited: set[str] = set(start_nodes)
        queue = list(start_nodes)

        for _ in range(depth):
            next_queue = []
            for node in queue:
                # Add successors (outgoing links)
                if node in self.graph:
                    for succ in self.graph.successors(node):
                        if succ not in visited:
                            visited.add(succ)
                            next_queue.append(succ)
                    # Add predecessors (incoming links)
                    for pred in self.graph.predecessors(node):
                        if pred not in visited:
                            visited.add(pred)
                            next_queue.append(pred)
            queue = next_queue

        return list(visited)

    def get_dependencies(self, file_path: str) -> list[str]:
        """Returns files imported by the given file."""
        rel = self._to_rel_path(file_path)
        if rel not in self.graph:
            return []

        deps = []
        for _, target, data in self.graph.out_edges(rel, data=True):
            if data.get("edge_type") == "imports":
                deps.append(target)
        return deps

    def get_dependents(self, file_path: str) -> list[str]:
        """Returns files that import the given file."""
        rel = self._to_rel_path(file_path)
        if rel not in self.graph:
            return []

        dependents = []
        for source, _, data in self.graph.in_edges(rel, data=True):
            if data.get("edge_type") == "imports":
                dependents.append(source)
        return dependents

    def _to_rel_path(self, path_str: str) -> str:
        """Helper to ensure paths are represented as uniform, workspace-relative strings."""
        if not path_str or not (path_str.startswith("/") or path_str.startswith("\\") or ":" in path_str):
            # Already relative or a symbol name
            return path_str.replace("\\", "/")

        try:
            p = Path(path_str).resolve()
            rel = p.relative_to(self.root_path)
            return str(rel).replace("\\", "/")
        except (ValueError, RuntimeError):
            return path_str.replace("\\", "/")
