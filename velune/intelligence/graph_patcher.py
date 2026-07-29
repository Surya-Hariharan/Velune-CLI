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
import time
from dataclasses import dataclass
from pathlib import Path

from velune.knowledge.graph import KnowledgeGraph
from velune.knowledge.schemas import EdgeType, KnowledgeEdge, KnowledgeNode, NodeType
from velune.repository.incremental_indexer import IndexDelta
from velune.repository.schemas import RepositorySymbolKind
from velune.repository.tracker import GitTracker

logger = logging.getLogger("velune.intelligence.graph_patcher")

# Churn-weighted confidence decay for FILE nodes (docs/
# repository-intelligence-baseline.md's Repository Evolution discussion:
# "churn-weighted decay applied to confidence for capability claims
# resting on recently-rewritten code"). _CHURN_DECAY_SCALE is the commit
# count (within the volatility window) at which confidence has halved;
# _CHURN_CONFIDENCE_FLOOR bounds how far decay can go — a highly volatile
# file's mere *existence* is still certain, only whether its current
# structure is "settled" is in question.
_CHURN_DECAY_SCALE = 8.0
_CHURN_CONFIDENCE_FLOOR = 0.5
_VOLATILITY_CACHE_TTL_SECONDS = 600.0

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
        self._tracker = GitTracker(self._workspace_root)
        self._volatility_cache: tuple[float, dict[str, int]] | None = None

    def _get_volatility_cached(self) -> dict[str, int]:
        """TTL-cached ``GitTracker.get_all_file_volatility`` — reflects commit
        history, not local edits, so recomputing it on every patch (every
        few seconds while the tree is dirty) would be pure waste. Not
        locked against concurrent access from parallel ``_parse_file``
        threads; a stale-cache race just means an occasional redundant
        git-log call, not an incorrect one.
        """
        now = time.time()
        if self._volatility_cache is not None:
            cached_at, data = self._volatility_cache
            if now - cached_at < _VOLATILITY_CACHE_TTL_SECONDS:
                return data
        data = self._tracker.get_all_file_volatility(days=90)
        self._volatility_cache = (now, data)
        return data

    def _churn_confidence(self, rel_path: str) -> float:
        """Confidence discount from how often *rel_path* has changed recently.

        A file rewritten frequently is still certain to exist and be
        language-tagged (that's not what's being discounted) — but its
        current structure is less likely to be "the settled design," so a
        capability/architecture claim resting on it should be held more
        loosely than one resting on stable, rarely-touched code.
        """
        commit_count = self._get_volatility_cached().get(rel_path, 0)
        decayed = 1.0 / (1.0 + commit_count / _CHURN_DECAY_SCALE)
        return max(_CHURN_CONFIDENCE_FLOOR, decayed)

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

        # A rename's new path still appears in delta.to_add (see
        # IndexDelta.renames' docstring — it's an additive, informational
        # overlay, not a rewrite of to_add/to_remove) — this just tells
        # _parse_file which old path a new one's node should record as its
        # git lineage.
        rename_source_of: dict[str, str] = {new: old for old, new in delta.renames}

        nodes_to_add: list[KnowledgeNode] = []
        edges_to_add: list[KnowledgeEdge] = []

        to_process = delta.to_add + delta.to_update
        if to_process:
            # Parse files concurrently (bounded by to_thread)
            parse_tasks = [
                asyncio.create_task(
                    asyncio.to_thread(self._parse_file, rel_path, rename_source_of.get(rel_path)),
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
            valid_edges = await self._drop_edges_to_unknown_targets(nodes_to_add, edges_to_add)
            removed = await self._graph.apply_patch(
                remove_files=remove_files, nodes=nodes_to_add, edges=valid_edges
            )
            result.nodes_removed += removed
            result.nodes_added += len(nodes_to_add)
            result.edges_added += len(valid_edges)

        logger.info(
            "KnowledgeGraph patched: +%d/-%d nodes, +%d edges across %d files (%d errors)",
            result.nodes_added,
            result.nodes_removed,
            result.edges_added,
            result.files_patched,
            result.errors,
        )
        return result

    async def _drop_edges_to_unknown_targets(
        self, nodes_to_add: list[KnowledgeNode], edges_to_add: list[KnowledgeEdge]
    ) -> list[KnowledgeEdge]:
        """Filter out edges whose target isn't a real node.

        Each file is parsed independently here (unlike
        ``RepositoryGrapher.resolve_import_dependencies``, which receives
        the *entire* repo's file list precisely so it can resolve a dotted
        import name or a dynamic-import prefix to a concrete file). A
        one-file-at-a-time parse has no such list to check against, so an
        edge's target — a stdlib/third-party module name for an ordinary
        static import, or an unresolved literal prefix for a dynamic one
        (e.g. "plugins", not "plugins/plugin_a.py") — often isn't a real
        node at all. kg_edges has a foreign-key constraint on both
        endpoints, so inserting one of these unconditionally raised
        ``IntegrityError`` and aborted the whole (now-atomic, see
        ``KnowledgeGraph.apply_patch``) transaction. Dropping the edge
        rather than fabricating a node for an unresolved reference is the
        same "an absent edge beats a wrong one" principle
        ``_extract_dynamic_imports`` already applies one layer up.
        """
        if not edges_to_add:
            return []
        known_ids = {n.id for n in nodes_to_add}
        unresolved = {e.target for e in edges_to_add if e.target not in known_ids}
        if unresolved:
            known_ids |= await self._graph.existing_node_ids(list(unresolved))
        return [e for e in edges_to_add if e.target in known_ids]

    # ------------------------------------------------------------------
    # Internal: synchronous parse (runs in thread pool)
    # ------------------------------------------------------------------

    def _parse_file(
        self, rel_path: str, renamed_from: str | None = None
    ) -> tuple[list[KnowledgeNode], list[KnowledgeEdge]] | None:
        """Parse a single file and return KnowledgeNodes + KnowledgeEdges.

        Returns None when the file does not exist (not an error; the caller
        should skip it without incrementing files_patched or errors).
        Runs synchronously — callers must wrap with ``asyncio.to_thread``.

        Files over ``MAX_STRUCTURAL_PARSE_BYTES`` get a file node only (no
        symbol nodes/edges) and are never read for structural parsing — the
        same opaque-file guard applied at the repository-indexer layer,
        needed here too since the KG patcher parses independently.

        *renamed_from*, when given (see ``IncrementalIndexer._detect_renames``),
        is recorded as git-lineage metadata on the file node. It is *not* a
        graph edge to the old path's node: that node is deleted in the same
        transaction (the old file no longer exists), and kg_edges' foreign-
        key constraint means an edge can't reference a node that won't
        exist once the transaction commits. Metadata records the lineage
        fact without requiring the old identity to persist as a live node.
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
                    # Present and language-tagged, but deliberately never
                    # read — the node asserts less than a fully-parsed file
                    # would (no symbol children), so it carries less
                    # confidence too.
                    confidence=0.5,
                    provenance=["opaque_size_guard"],
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
                    confidence=0.5,
                    provenance=["generated_marker"],
                )
            ], []

        try:
            symbols, repo_edges = parser.parse(abs_path, content)
        except Exception as exc:
            logger.debug("Parser error on %s: %s", rel_path, exc)
            return [], []

        nodes: list[KnowledgeNode] = []
        edges: list[KnowledgeEdge] = []

        # File node. Confidence is churn-discounted: a frequently-rewritten
        # file's current structure is less likely to be "the settled
        # design" than stable, rarely-touched code (see _churn_confidence).
        file_metadata: dict = {"language": lang.value, "size_bytes": size_bytes}
        file_provenance = ["structural_parse"]
        if renamed_from is not None:
            # Git lineage, recorded as metadata rather than a graph edge —
            # see the docstring above for why a live EVOLVED_FROM edge to
            # the (about to be deleted) old path's node isn't possible here.
            file_metadata["renamed_from"] = renamed_from
            file_provenance.append("rename_lineage")

        nodes.append(
            KnowledgeNode(
                id=file_nid,
                node_type=NodeType.FILE,
                label=rel_path,
                file_path=rel_path,
                metadata=file_metadata,
                confidence=self._churn_confidence(rel_path),
                provenance=file_provenance,
            )
        )

        # Symbol nodes + DEFINES edges
        for sym in symbols:
            node_type = _KIND_TO_NODE_TYPE.get(sym.kind, NodeType.FUNCTION)
            nid = sym.symbol_id or f"sym:{rel_path}:{sym.name}"
            # A symbol from the universal fallback extractor (any language
            # without a dedicated AST/tree-sitter/regex pattern set — see
            # parser._GENERIC_FALLBACK_PATTERNS) is a heuristic keyword
            # match, not a grammar-verified structural fact; score it lower
            # accordingly instead of presenting it as equally certain.
            is_generic_fallback = sym.metadata.get("extraction") == "generic_fallback"
            is_dynamic = bool(sym.metadata.get("dynamic"))
            confidence = float(
                sym.metadata.get("resolution_confidence", 0.5 if is_generic_fallback else 1.0)
            )
            provenance = (
                ["generic_fallback_regex"]
                if is_generic_fallback
                else (["dynamic_import"] if is_dynamic else ["structural_parse"])
            )
            nodes.append(
                KnowledgeNode(
                    id=nid,
                    node_type=node_type,
                    label=sym.name,
                    file_path=rel_path,
                    line_start=sym.line_start,
                    line_end=sym.line_end,
                    metadata={"qualified_name": sym.qualified_name or sym.name},
                    confidence=confidence,
                    provenance=provenance,
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
                        # weight already carries a dynamic-import's
                        # resolution confidence (see
                        # RepositorySnapshotParser._classify_dynamic_import_arg)
                        # — mirrored here so a caller reading .confidence
                        # doesn't need to know weight means the same thing
                        # for this particular edge type.
                        confidence=repo_edge.weight,
                        provenance=[repo_edge.edge_type],
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
