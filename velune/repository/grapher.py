"""Dependency and import grapher using networkx."""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


from velune.repository.schemas import RepositoryEdge, RepositorySymbol, RepositorySymbolKind


class RepositoryGrapher:
    """Builds and analyzes dependency graphs for repository files and symbols."""

    def __init__(self, root_path: Path) -> None:
        import networkx as nx

        self.root_path = root_path.resolve()
        self.graph = nx.MultiDiGraph()

    def add_file(self, file_path: str, language: str, size_bytes: int) -> None:
        """Adds a file node to the dependency graph."""
        # Normalize paths relative to workspace
        rel_path = self._to_rel_path(file_path)
        self.graph.add_node(
            rel_path, kind="file", language=language, size_bytes=size_bytes, type="file"
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
            type="symbol",
        )

        # Draw containing relationship
        self.graph.add_edge(file_rel, sym_id, edge_type="contains", weight=1.0)

    def remove_file(self, file_path: str) -> None:
        """Remove a file node, its contained symbol nodes, and all incident edges.

        Used to patch a persistent graph in place for ``delta.to_remove`` files
        instead of rebuilding the whole graph from scratch.
        """
        rel_path = self._to_rel_path(file_path)
        if rel_path not in self.graph:
            return

        contained_symbols = [
            target
            for _, target, data in self.graph.out_edges(rel_path, data=True)
            if data.get("edge_type") == "contains"
        ]
        self.graph.remove_node(rel_path)
        for sym_id in contained_symbols:
            if sym_id in self.graph:
                self.graph.remove_node(sym_id)

    def add_edge(self, edge: RepositoryEdge) -> None:
        """Adds a relationship edge between files or symbols."""
        source_rel = self._to_rel_path(edge.source)
        target_rel = self._to_rel_path(edge.target)
        self.graph.add_edge(source_rel, target_rel, edge_type=edge.edge_type, weight=edge.weight)

    def resolve_import_dependencies(
        self,
        files: list[str],
        symbols: list[RepositorySymbol],
        source_scope: set[str] | None = None,
    ) -> None:
        """Resolves module imports to concrete files and draws file-to-file import edges.

        *files* is always the full file list — the stem/module lookup maps need
        every file to resolve targets correctly, and building them is pure
        string manipulation (no I/O), so passing the full list every call is
        cheap regardless of repo size.

        *source_scope*, when given, restricts which files' own import symbols
        get processed — pass ``delta.to_add + delta.to_update`` here to add
        edges only for files that actually changed, leaving edges from
        unchanged files (already in the persistent graph) untouched.

        Drops each scoped file's existing outgoing ``imports`` edges first, so
        re-running this on an updated file replaces its stale import edges
        rather than accumulating duplicates alongside the new ones.
        """
        if source_scope is not None:
            for src in source_scope:
                rel_src = self._to_rel_path(src)
                if rel_src not in self.graph:
                    continue
                stale = [
                    (rel_src, tgt, key)
                    for _, tgt, key, data in self.graph.out_edges(rel_src, keys=True, data=True)
                    if data.get("edge_type") == "imports"
                ]
                self.graph.remove_edges_from(stale)

        _code_exts = {".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs"}

        # Build two lookup maps:
        #   file_by_stem: path-without-extension → rel_path  (e.g. "src/screens/Login" → "src/screens/Login.tsx")
        #   file_by_mod:  dotted module name     → rel_path  (e.g. "velune.kernel.bus" → "velune/kernel/bus.py")
        file_by_stem: dict[str, str] = {}
        file_by_mod: dict[str, str] = {}

        for f in files:
            rel = self._to_rel_path(f)
            p = Path(rel.replace("\\", "/"))
            if p.suffix.lower() in _code_exts:
                stem = str(p.with_suffix("")).replace("\\", "/")
                file_by_stem[stem] = rel
                # Dotted module name for Python-style resolution
                mod_name = stem.replace("/", ".")
                file_by_mod[mod_name] = rel

            # index / __init__ files: parent directory resolves to them
            if p.name.lower() in ("index.ts", "index.tsx", "index.js", "index.jsx", "__init__.py"):
                parent = str(p.parent).replace("\\", "/")
                if parent and parent != ".":
                    file_by_stem[parent] = rel
                    file_by_mod[parent.replace("/", ".")] = rel

        for sym in symbols:
            if sym.kind != RepositorySymbolKind.IMPORT:
                continue

            source_file = self._to_rel_path(sym.file_path)
            if source_scope is not None and source_file not in source_scope:
                continue

            import_name = sym.name

            matched_file: str | None = None

            if import_name.startswith("."):
                # Relative import: "../../screens/LoginScreen", "./Button", "../utils/theme"
                resolved_stem = self._resolve_relative_path(source_file, import_name)
                matched_file = file_by_stem.get(resolved_stem)
            else:
                # Try dotted Python module name (velune.kernel.bus)
                matched_file = file_by_mod.get(import_name)

                # Try metadata module for Python from-imports
                if not matched_file and "module" in sym.metadata:
                    mod = sym.metadata["module"]
                    matched_file = file_by_mod.get(mod)
                    if not matched_file:
                        stem = mod.replace(".", "/")
                        matched_file = file_by_stem.get(stem)

            if matched_file and source_file != matched_file:
                self.graph.add_edge(source_file, matched_file, edge_type="imports", weight=1.0)

    def _resolve_relative_path(self, source_file: str, import_path: str) -> str:
        """Resolve a relative import (starting with . or ..) to a workspace-relative stem.

        Example:
            source_file = "src/app/(tabs)/_layout.tsx"
            import_path = "../../screens/LoginScreen"
            → "src/screens/LoginScreen"
        """
        source_norm = source_file.replace("\\", "/")
        # Start at the directory containing source_file
        dir_parts = source_norm.split("/")[:-1]

        for segment in import_path.split("/"):
            if segment == "..":
                if dir_parts:
                    dir_parts.pop()
            elif segment in (".", ""):
                pass
            else:
                dir_parts.append(segment)

        return "/".join(dir_parts)

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
        if not path_str or not (
            path_str.startswith("/") or path_str.startswith("\\") or ":" in path_str
        ):
            # Already relative or a symbol name
            return path_str.replace("\\", "/")

        try:
            p = Path(path_str).resolve()
            rel = p.relative_to(self.root_path)
            return str(rel).replace("\\", "/")
        except (ValueError, RuntimeError):
            return path_str.replace("\\", "/")
