"""AI-optimized query interface for the Repository Knowledge Graph.

Provides higher-level queries that return structured context ready for
insertion into an LLM prompt. Methods here must be fast (sub-50 ms on
typical repos) and return well-bounded results.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from velune.knowledge.graph import KnowledgeGraph
from velune.knowledge.schemas import EdgeType, KnowledgeEdge, KnowledgeNode, NodeType

logger = logging.getLogger("velune.knowledge.query")


@dataclass
class FileContext:
    """AI context block for a single source file."""

    file_path: str
    file_node: KnowledgeNode | None
    symbols: list[KnowledgeNode] = field(default_factory=list)
    imports_from: list[str] = field(default_factory=list)
    imported_by: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        """Render as a compact text block suitable for an LLM prompt."""
        lines: list[str] = [f"File: {self.file_path}"]
        if self.symbols:
            sym_names = [s.label for s in self.symbols]
            lines.append(f"  Defines: {', '.join(sym_names)}")
        if self.imports_from:
            lines.append(f"  Imports: {', '.join(self.imports_from[:10])}")
        if self.imported_by:
            lines.append(f"  Imported by: {', '.join(self.imported_by[:5])}")
        return "\n".join(lines)


@dataclass
class SubgraphContext:
    """AI context block for a neighbourhood subgraph."""

    focus_id: str
    nodes: list[KnowledgeNode] = field(default_factory=list)
    edges: list[KnowledgeEdge] = field(default_factory=list)

    def as_text(self) -> str:
        """Render a compact summary of the subgraph."""
        lines: list[str] = [f"Subgraph around: {self.focus_id}"]
        for node in self.nodes[:20]:
            lines.append(f"  [{node.node_type.value}] {node.label} ({node.file_path or ''})")
        if len(self.nodes) > 20:
            lines.append(f"  ... and {len(self.nodes) - 20} more nodes")
        return "\n".join(lines)


class KnowledgeQuery:
    """Higher-level AI-optimized queries over a KnowledgeGraph.

    Wraps the low-level graph I/O in domain-meaningful calls whose
    return types are directly usable by context assembly pipelines.
    """

    def __init__(self, graph: KnowledgeGraph) -> None:
        self._graph = graph

    # ------------------------------------------------------------------
    # File-centric queries
    # ------------------------------------------------------------------

    async def context_for_file(self, file_path: str) -> FileContext:
        """Return a FileContext containing symbols and import relationships.

        Parameters
        ----------
        file_path:
            The relative file path (as stored in the graph, e.g. ``velune/foo.py``).
        """
        file_nid = f"file:{file_path}"
        file_node = await self._graph.get_node(file_nid)

        # Symbols defined in this file
        symbol_nodes = await self._graph.get_nodes_by_file(file_path)
        symbols = [n for n in symbol_nodes if n.node_type != NodeType.FILE]

        # Files this file imports
        out_neighbors = await self._graph.neighbors(
            file_nid, edge_type=EdgeType.IMPORTS, direction="out"
        )
        imports_from = [
            n.file_path or n.label for n, _ in out_neighbors if n.node_type == NodeType.FILE
        ]

        # Files that import this file
        in_neighbors = await self._graph.neighbors(
            file_nid, edge_type=EdgeType.IMPORTS, direction="in"
        )
        imported_by = [
            n.file_path or n.label for n, _ in in_neighbors if n.node_type == NodeType.FILE
        ]

        return FileContext(
            file_path=file_path,
            file_node=file_node,
            symbols=symbols,
            imports_from=imports_from,
            imported_by=imported_by,
        )

    async def context_for_files(self, file_paths: list[str]) -> list[FileContext]:
        """Batch version of context_for_file."""
        results = []
        for path in file_paths:
            ctx = await self.context_for_file(path)
            results.append(ctx)
        return results

    # ------------------------------------------------------------------
    # Symbol-centric queries
    # ------------------------------------------------------------------

    async def subgraph_for_symbol(self, symbol_id: str, depth: int = 2) -> SubgraphContext:
        """Return a subgraph centered on a symbol node.

        Parameters
        ----------
        symbol_id:
            The stable symbol_id as stored in the graph.
        depth:
            BFS depth; 2 is usually enough for AI context.
        """
        nodes, edges = await self._graph.subgraph(symbol_id, depth=depth)
        return SubgraphContext(focus_id=symbol_id, nodes=nodes, edges=edges)

    async def importers_of(self, file_path: str) -> list[str]:
        """Return relative paths of files that import the given file."""
        file_nid = f"file:{file_path}"
        in_nbrs = await self._graph.neighbors(file_nid, edge_type=EdgeType.IMPORTS, direction="in")
        return [n.file_path or n.label for n, _ in in_nbrs]

    async def dependencies_of(self, file_path: str) -> list[str]:
        """Return relative paths of files imported by the given file."""
        file_nid = f"file:{file_path}"
        out_nbrs = await self._graph.neighbors(
            file_nid, edge_type=EdgeType.IMPORTS, direction="out"
        )
        return [n.file_path or n.label for n, _ in out_nbrs]

    # ------------------------------------------------------------------
    # Cross-cutting queries
    # ------------------------------------------------------------------

    async def find_classes(self) -> list[KnowledgeNode]:
        """Return all class nodes in the graph."""
        return await self._graph.get_nodes_by_type(NodeType.CLASS)

    async def find_functions(self) -> list[KnowledgeNode]:
        """Return all function nodes (excluding methods)."""
        return await self._graph.get_nodes_by_type(NodeType.FUNCTION)

    async def find_by_label(self, label: str) -> list[KnowledgeNode]:
        """Case-insensitive prefix search over node labels.

        Implemented with SQLite LIKE for simplicity; fast enough for
        interactive completion (<5k nodes is the common case).
        """

        pattern = f"{label.lower()}%"
        async with self._graph._read() as conn:  # noqa: SLF001
            cursor = await conn.execute(
                "SELECT * FROM kg_nodes WHERE LOWER(label) LIKE ? LIMIT 50",
                (pattern,),
            )
            rows = await cursor.fetchall()
        from velune.knowledge.graph import _row_to_node

        return [_row_to_node(r) for r in rows]

    async def summary_text(self) -> str:
        """One-paragraph natural language summary of the graph for use in a system prompt."""
        stats = await self._graph.stats()
        if stats.node_count == 0:
            return "No repository knowledge graph available. Run /cognition to index the workspace."
        return (
            f"Repository knowledge graph: {stats.file_count} files, "
            f"{stats.symbol_count} symbols ({stats.node_count} total nodes, "
            f"{stats.edge_count} relationships). "
            f"Root: {stats.root_path or 'unknown'}."
        )
