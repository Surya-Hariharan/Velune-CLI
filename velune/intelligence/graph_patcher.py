"""Surgical incremental patcher for the Repository Knowledge Graph.

Applies an IndexDelta to an existing KnowledgeGraph without a full rebuild:

* Files in ``delta.to_remove`` → delete their nodes (CASCADE removes edges).
* Files in ``delta.to_add / to_update`` → re-parse, delete old nodes, upsert fresh.

This keeps graph updates proportional to the size of the change, not the size
of the repository.  A 1-file edit touches only that file's nodes and edges.

The patcher is intentionally stateless — create once per engine, call for
every delta.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from velune.knowledge.graph import KnowledgeGraph
from velune.knowledge.schemas import EdgeType, KnowledgeEdge, KnowledgeNode, NodeType
from velune.repository.incremental_indexer import IndexDelta
from velune.repository.schemas import RepositorySymbolKind

logger = logging.getLogger("velune.intelligence.graph_patcher")

_KIND_TO_NODE_TYPE = {
    RepositorySymbolKind.CLASS: NodeType.CLASS,
    RepositorySymbolKind.FUNCTION: NodeType.FUNCTION,
    RepositorySymbolKind.METHOD: NodeType.METHOD,
    RepositorySymbolKind.IMPORT: NodeType.MODULE,
    RepositorySymbolKind.UNKNOWN: NodeType.FUNCTION,
}


@dataclass
class PatchResult:
    nodes_added: int = 0
    nodes_removed: int = 0
    edges_added: int = 0
    files_patched: int = 0
    errors: int = 0


class KnowledgeGraphPatcher:
    """Applies IndexDelta changes surgically to a KnowledgeGraph.

    Usage::

        patcher = KnowledgeGraphPatcher(knowledge_graph, workspace_root)
        result = await patcher.patch(delta)
    """

    def __init__(self, graph: KnowledgeGraph, workspace_root: Path) -> None:
        self._graph = graph
        self._workspace_root = workspace_root.resolve()

    async def patch(self, delta: IndexDelta) -> PatchResult:
        """Apply delta to the knowledge graph. Returns a PatchResult."""
        result = PatchResult()

        if delta.is_empty:
            return result

        # Files whose nodes must be removed before this patch commits: fully
        # deleted files, plus updated files' stale nodes (purged ahead of the
        # fresh upsert). Combined into one call so the whole delta — removal,
        # node upsert, edge upsert — applies as a single transaction; see
        # KnowledgeGraph.apply_patch for why that matters (a crash mid-patch
        # must not leave deleted-but-not-reinserted nodes).
        remove_files = list(delta.to_remove) + list(delta.to_update)

        nodes_to_add: list[KnowledgeNode] = []
        edges_to_add: list[KnowledgeEdge] = []

        to_process = delta.to_add + delta.to_update
        if to_process:
            # Parse files concurrently (bounded by to_thread)
            parse_tasks = [
                asyncio.create_task(
                    asyncio.to_thread(self._parse_file, rel_path),
                    name=f"kg-patch-{rel_path}",
                )
                for rel_path in to_process
            ]
            parse_results = await asyncio.gather(*parse_tasks, return_exceptions=True)

            for rel_path, parsed in zip(to_process, parse_results, strict=False):
                if isinstance(parsed, Exception):
                    logger.debug("Patcher: parse failed for %s: %s", rel_path, parsed)
                    result.errors += 1
                    continue
                if parsed is None:
                    # File does not exist on disk — skip silently
                    continue

                file_nodes, file_edges = parsed
                nodes_to_add.extend(file_nodes)
                edges_to_add.extend(file_edges)
                result.files_patched += 1

        if remove_files or nodes_to_add or edges_to_add:
            removed = await self._graph.apply_patch(
                remove_files=remove_files, nodes=nodes_to_add, edges=edges_to_add
            )
            result.nodes_removed += removed
            result.nodes_added += len(nodes_to_add)
            result.edges_added += len(edges_to_add)

        logger.info(
            "KnowledgeGraph patched: +%d/-%d nodes, +%d edges across %d files (%d errors)",
            result.nodes_added,
            result.nodes_removed,
            result.edges_added,
            result.files_patched,
            result.errors,
        )
        return result

    # ------------------------------------------------------------------
    # Internal: synchronous parse (runs in thread pool)
    # ------------------------------------------------------------------

    def _parse_file(self, rel_path: str) -> tuple[list[KnowledgeNode], list[KnowledgeEdge]] | None:
        """Parse a single file and return KnowledgeNodes + KnowledgeEdges.

        Returns None when the file does not exist (not an error; the caller
        should skip it without incrementing files_patched or errors).
        Runs synchronously — callers must wrap with ``asyncio.to_thread``.

        Files over ``MAX_STRUCTURAL_PARSE_BYTES`` get a file node only (no
        symbol nodes/edges) and are never read for structural parsing — the
        same opaque-file guard applied at the repository-indexer layer,
        needed here too since the KG patcher parses independently.
        """
        from velune.repository.parser import RepositorySnapshotParser
        from velune.repository.schemas import MAX_STRUCTURAL_PARSE_BYTES, is_generated_content

        abs_path = self._workspace_root / rel_path
        if not abs_path.exists():
            return None

        parser = RepositorySnapshotParser()
        lang = parser._detect_language(abs_path)
        size_bytes = abs_path.stat().st_size
        file_nid = f"file:{rel_path}"

        if size_bytes > MAX_STRUCTURAL_PARSE_BYTES:
            return [
                KnowledgeNode(
                    id=file_nid,
                    node_type=NodeType.FILE,
                    label=rel_path,
                    file_path=rel_path,
                    metadata={"language": lang.value, "size_bytes": size_bytes, "opaque": True},
                )
            ], []

        try:
            content = abs_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None

        if is_generated_content(content):
            return [
                KnowledgeNode(
                    id=file_nid,
                    node_type=NodeType.FILE,
                    label=rel_path,
                    file_path=rel_path,
                    metadata={"language": lang.value, "size_bytes": size_bytes, "generated": True},
                )
            ], []

        try:
            symbols, repo_edges = parser.parse(abs_path, content)
        except Exception as exc:
            logger.debug("Parser error on %s: %s", rel_path, exc)
            return [], []

        nodes: list[KnowledgeNode] = []
        edges: list[KnowledgeEdge] = []

        # File node
        nodes.append(
            KnowledgeNode(
                id=file_nid,
                node_type=NodeType.FILE,
                label=rel_path,
                file_path=rel_path,
                metadata={"language": lang.value, "size_bytes": size_bytes},
            )
        )

        # Symbol nodes + DEFINES edges
        for sym in symbols:
            node_type = _KIND_TO_NODE_TYPE.get(sym.kind, NodeType.FUNCTION)
            nid = sym.symbol_id or f"sym:{rel_path}:{sym.name}"
            nodes.append(
                KnowledgeNode(
                    id=nid,
                    node_type=node_type,
                    label=sym.name,
                    file_path=rel_path,
                    line_start=sym.line_start,
                    line_end=sym.line_end,
                    metadata={"qualified_name": sym.qualified_name or sym.name},
                )
            )
            edges.append(KnowledgeEdge(source=file_nid, target=nid, edge_type=EdgeType.DEFINES))

            # CONTAINS for methods inside classes
            if sym.parent:
                parent_sym = next(
                    (s for s in symbols if s.name == sym.parent and s.file_path == sym.file_path),
                    None,
                )
                if parent_sym:
                    parent_nid = parent_sym.symbol_id or f"sym:{rel_path}:{parent_sym.name}"
                    edges.append(
                        KnowledgeEdge(
                            source=parent_nid,
                            target=nid,
                            edge_type=EdgeType.CONTAINS,
                        )
                    )

        # Repository-level edges from parser (imports etc.)
        for repo_edge in repo_edges:
            tgt_path = repo_edge.target
            tgt_nid = f"file:{tgt_path}"
            edge_type = _repo_edge_to_kg_type(repo_edge.edge_type)
            if edge_type is not None:
                edges.append(
                    KnowledgeEdge(
                        source=file_nid,
                        target=tgt_nid,
                        edge_type=edge_type,
                        weight=repo_edge.weight,
                    )
                )

        return nodes, edges


def _repo_edge_to_kg_type(raw: str) -> EdgeType | None:
    mapping: dict[str, EdgeType | None] = {
        "imports": EdgeType.IMPORTS,
        # A confidence-scored dynamic-import edge (see
        # RepositorySnapshotParser._extract_dynamic_imports) — the KG schema
        # has no separate dynamic-edge type yet, so this reuses IMPORTS
        # rather than silently dropping the edge; repo_edge.weight already
        # carries the lower confidence through to KnowledgeEdge.weight.
        "imports_dynamic": EdgeType.IMPORTS,
        "contains": EdgeType.CONTAINS,
        "inherits": EdgeType.INHERITS,
        "defines": EdgeType.DEFINES,
        "calls": None,
    }
    return mapping.get(raw.lower())
