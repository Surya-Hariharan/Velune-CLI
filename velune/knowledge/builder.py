"""Builds the Repository Knowledge Graph from existing repository analysis.

Takes a RepositorySnapshot (symbols, files, edges) produced by the existing
indexer pipeline, merges it with ImportGraphBuilder metrics, and writes the
unified semantic graph to KnowledgeGraph.

Ownership boundary:
  - velune/repository/ owns raw AST analysis (inputs here).
  - velune/knowledge/ owns the AI-queryable semantic graph (output here).
  - No business logic from repository/ is duplicated.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from velune.knowledge.graph import KnowledgeGraph
from velune.knowledge.schemas import EdgeType, KnowledgeEdge, KnowledgeNode, NodeType
from velune.repository.import_graph import ImportGraphBuilder, ImportMetrics
from velune.repository.schemas import (
    RepositorySnapshot,
    RepositorySymbolKind,
)

logger = logging.getLogger("velune.knowledge.builder")

# Map repository symbol kinds to knowledge node types
_KIND_TO_NODE_TYPE: dict[str, NodeType] = {
    RepositorySymbolKind.CLASS: NodeType.CLASS,
    RepositorySymbolKind.FUNCTION: NodeType.FUNCTION,
    RepositorySymbolKind.METHOD: NodeType.METHOD,
    RepositorySymbolKind.IMPORT: NodeType.MODULE,
    RepositorySymbolKind.UNKNOWN: NodeType.FUNCTION,
}


class KnowledgeGraphBuilder:
    """Converts a RepositorySnapshot and import metrics into a KnowledgeGraph.

    Usage
    -----
    ::

        graph = KnowledgeGraph(db_path)
        await graph.initialize()

        builder = KnowledgeGraphBuilder(graph)
        stats = await builder.build(snapshot, root_path)
    """

    def __init__(self, graph: KnowledgeGraph) -> None:
        self._graph = graph

    async def build(
        self,
        snapshot: RepositorySnapshot,
        root_path: Path,
        *,
        clear_first: bool = True,
    ) -> None:
        """Populate the KnowledgeGraph from snapshot data.

        Parameters
        ----------
        snapshot:
            The RepositorySnapshot produced by RepositorySnapshotParser or
            the incremental indexer.
        root_path:
            The workspace root, used for import graph scanning.
        clear_first:
            When True (default) wipes existing nodes/edges before writing so
            the graph stays consistent with the current snapshot.
        """
        t0 = time.monotonic()
        if clear_first:
            await self._graph.clear()

        nodes: list[KnowledgeNode] = []
        edges: list[KnowledgeEdge] = []

        # 1. File nodes
        file_node_ids: dict[str, str] = {}  # path → node_id
        for repo_file in snapshot.files:
            nid = f"file:{repo_file.path}"
            file_node_ids[repo_file.path] = nid
            nodes.append(
                KnowledgeNode(
                    id=nid,
                    node_type=NodeType.FILE,
                    label=repo_file.path,
                    file_path=repo_file.path,
                    metadata={
                        "language": repo_file.language.value,
                        "size_bytes": repo_file.size_bytes,
                        "sha256": repo_file.sha256,
                    },
                )
            )

        # 2. Symbol nodes + DEFINES edges (file → symbol)
        for symbol in snapshot.symbols:
            node_type = _KIND_TO_NODE_TYPE.get(symbol.kind, NodeType.FUNCTION)
            nid = symbol.symbol_id or f"sym:{symbol.file_path}:{symbol.name}"
            nodes.append(
                KnowledgeNode(
                    id=nid,
                    node_type=node_type,
                    label=symbol.name,
                    file_path=symbol.file_path,
                    line_start=symbol.line_start,
                    line_end=symbol.line_end,
                    metadata={
                        "qualified_name": symbol.qualified_name or symbol.name,
                        "parent": symbol.parent,
                        **(symbol.metadata or {}),
                    },
                )
            )

            # DEFINES: file → symbol
            file_nid = file_node_ids.get(symbol.file_path, f"file:{symbol.file_path}")
            edges.append(
                KnowledgeEdge(
                    source=file_nid,
                    target=nid,
                    edge_type=EdgeType.DEFINES,
                )
            )

            # CONTAINS: parent_symbol → child_symbol (methods inside classes)
            if symbol.parent:
                parent_sym = next(
                    (
                        s
                        for s in snapshot.symbols
                        if s.name == symbol.parent and s.file_path == symbol.file_path
                    ),
                    None,
                )
                if parent_sym:
                    parent_nid = (
                        parent_sym.symbol_id or f"sym:{parent_sym.file_path}:{parent_sym.name}"
                    )
                    edges.append(
                        KnowledgeEdge(
                            source=parent_nid,
                            target=nid,
                            edge_type=EdgeType.CONTAINS,
                        )
                    )

        # 3. Repository-level edges (from RepositorySnapshot.edges)
        for repo_edge in snapshot.edges:
            src = file_node_ids.get(repo_edge.source, f"file:{repo_edge.source}")
            tgt = file_node_ids.get(repo_edge.target, f"file:{repo_edge.target}")
            edge_type = _map_edge_type(repo_edge.edge_type)
            if edge_type is not None:
                edges.append(
                    KnowledgeEdge(
                        source=src,
                        target=tgt,
                        edge_type=edge_type,
                        weight=repo_edge.weight,
                    )
                )

        # 4. Import graph — adds IMPORTS edges between files
        import_metrics = _build_import_metrics(root_path)
        for rel_path, metrics in import_metrics.items():
            src_nid = f"file:{rel_path}"
            for imported in metrics.imports:
                # Only link to files that exist in the snapshot to avoid phantom nodes
                if imported in file_node_ids:
                    tgt_nid = file_node_ids[imported]
                    edges.append(
                        KnowledgeEdge(
                            source=src_nid,
                            target=tgt_nid,
                            edge_type=EdgeType.IMPORTS,
                            metadata={"fan_in": metrics.fan_in, "fan_out": metrics.fan_out},
                        )
                    )

        # 5. Flush to storage
        await self._graph.upsert_nodes_bulk(nodes)
        await self._graph.upsert_edges_bulk(edges)

        elapsed = time.monotonic() - t0
        await self._graph.set_meta("root_path", str(root_path))
        await self._graph.set_meta("built_at", str(time.time()))

        logger.info(
            "KnowledgeGraph built: %d nodes, %d edges in %.2fs",
            len(nodes),
            len(edges),
            elapsed,
        )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _build_import_metrics(root_path: Path) -> dict[str, ImportMetrics]:
    """Run ImportGraphBuilder synchronously and return the metrics map."""
    try:
        builder = ImportGraphBuilder()
        return builder.build_from_directory(root_path)
    except Exception as exc:
        logger.warning("ImportGraphBuilder failed for %s: %s", root_path, exc)
        return {}


def _map_edge_type(raw: str) -> EdgeType | None:
    """Map a RepositoryEdge.edge_type string to a KnowledgeGraph EdgeType."""
    mapping = {
        "imports": EdgeType.IMPORTS,
        "contains": EdgeType.CONTAINS,
        "inherits": EdgeType.INHERITS,
        "defines": EdgeType.DEFINES,
        "calls": None,  # Not yet modelled; would require call-graph analysis
    }
    return mapping.get(raw.lower())
