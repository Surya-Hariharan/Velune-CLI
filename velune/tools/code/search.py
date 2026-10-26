from __future__ import annotations

import asyncio
import re
import threading
import time
from pathlib import Path
from typing import Any

from velune.execution.path_guard import resolve_in_workspace
from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.code._container import resolve_container

#: Limits of the ``symbol_search`` fallback scan (the graph path is unaffected).
SYMBOL_SCAN_MAX_FILES = 5_000
SYMBOL_SCAN_MAX_RESULTS = 200
SYMBOL_SCAN_TIME_BUDGET_SECONDS = 10.0


def _limit_results(results: list, limit: int) -> list:
    """Cap *results* at *limit* entries, keeping (or adding) the truncation marker."""
    from velune.tools.filesystem.search import truncation_marker

    hits = [r for r in results if not (isinstance(r, dict) and r.get("truncated"))]
    marker = next((r for r in results if isinstance(r, dict) and r.get("truncated")), None)
    if len(hits) > limit:
        reasons = [f"limit={limit}"] + (marker["reasons"] if marker else [])
        return [*hits[:limit], truncation_marker(reasons)]
    return [*hits, marker] if marker else hits


class SemanticCodeSearch(BaseTool):
    """Tool for semantic code search."""

    def __init__(self, workspace: Path | None = None, container: Any | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        # The runtime's service container (explicit injection). ``None`` falls back
        # to the process-wide registry for standalone use.
        self._container = container

    def get_required_permissions(self) -> set[ToolPermission]:
        # Read-only: runs in every execution mode without approval.
        return {ToolPermission.FILESYSTEM_READ}

    def get_name(self) -> str:
        return "semantic_code_search"

    def get_description(self) -> str:
        return "Search code semantically"

    async def execute(
        self,
        query: str,
        directory: str = ".",
        limit: int = 10,
    ) -> list[dict]:
        """Search code semantically via the hybrid retriever (lexical + vector + graph).

        Falls back to a plain grep — this tool's original behavior — when no
        retriever is registered in the container (e.g. running outside a
        bootstrapped Velune session) or the retrieval call itself errors, so
        this tool degrades rather than fails when retrieval isn't available.
        """
        try:
            container = resolve_container(self._container)
            if container.has("runtime.retrieval"):
                retriever = container.get("runtime.retrieval")
                if retriever is not None:
                    return await self._semantic_search(retriever, query, limit)
        except Exception:
            pass

        return await self._grep_fallback(query, directory, limit)

    async def _semantic_search(self, retriever: Any, query: str, limit: int) -> list[dict]:
        from velune.cognition.intent import IntentClassifier
        from velune.retrieval.planner import RetrievalPlanner

        intent, confidence = IntentClassifier().classify_with_confidence(query)
        planner = RetrievalPlanner()
        result = await planner.plan_and_retrieve(retriever, intent, confidence, query)

        results: list[dict] = []
        for hit in result.hits[:limit]:
            meta = hit.document.metadata or {}
            results.append(
                {
                    "path": meta.get("path") or meta.get("file_path") or hit.document.id,
                    "content": hit.document.content,
                    "score": hit.score,
                    "source": str(hit.source),
                }
            )
        return results

    async def _grep_fallback(self, query: str, directory: str, limit: int) -> list[dict]:
        from velune.tools.filesystem.search import GrepFiles

        grep = GrepFiles(workspace=self.workspace)
        results = await grep.execute(pattern=re.escape(query), directory=directory)
        return _limit_results(results, limit)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of results to return",
                },
            },
            "required": ["query"],
        }


class SymbolSearch(BaseTool):
    """Tool for searching symbols."""

    def __init__(self, workspace: Path | None = None, container: Any | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        # The runtime's service container (explicit injection). ``None`` falls back
        # to the process-wide registry for standalone use.
        self._container = container

    def get_required_permissions(self) -> set[ToolPermission]:
        # Read-only: runs in every execution mode without approval.
        return {ToolPermission.FILESYSTEM_READ}

    def get_name(self) -> str:
        return "symbol_search"

    def get_description(self) -> str:
        return "Search for symbols in code"

    async def execute(
        self,
        symbol_name: str,
        directory: str = ".",
    ) -> list[dict]:
        """Search for symbols.

        Tries the repository knowledge graph first — a cached, already-built
        index — so this doesn't re-parse the entire tree from scratch on
        every call. Falls back to a full scan (previously the only path,
        and still Python-only) when the graph is empty or unavailable, e.g.
        during Tier-1 warm-up or in an unindexed workspace.
        """
        graph_hits = await self._search_via_graph(symbol_name)
        if graph_hits is not None:
            return graph_hits

        root_path = resolve_in_workspace(directory, self.workspace, label="symbol_search")
        cancel = threading.Event()
        try:
            return await asyncio.to_thread(self._scan_python, root_path, symbol_name, cancel)
        except asyncio.CancelledError:
            cancel.set()
            raise

    def _scan_python(self, root_path: Path, symbol_name: str, cancel: threading.Event) -> list:
        """Bounded full scan -- the fallback when the knowledge graph is unavailable.

        Python files only; at most ``SYMBOL_SCAN_MAX_FILES`` files, none larger than
        the structural-parse limit, within ``SYMBOL_SCAN_TIME_BUDGET_SECONDS``.
        """
        from velune.repository.parser import RepositorySnapshotParser
        from velune.repository.scanner import FilesystemScanner
        from velune.repository.schemas import MAX_STRUCTURAL_PARSE_BYTES
        from velune.tools.filesystem.search import truncation_marker

        deadline = time.monotonic() + SYMBOL_SCAN_TIME_BUDGET_SECONDS
        files = FilesystemScanner(root_path).scan([".py"])
        parser = RepositorySnapshotParser()
        reasons: list[str] = []
        results: list[dict] = []
        skipped_large = 0

        for index, file_path in enumerate(files):
            if cancel.is_set():
                break
            if index >= SYMBOL_SCAN_MAX_FILES:
                reasons.append(f"file_limit={SYMBOL_SCAN_MAX_FILES}")
                break
            if time.monotonic() > deadline:
                reasons.append(f"time_budget={SYMBOL_SCAN_TIME_BUDGET_SECONDS:g}s")
                break
            try:
                if file_path.stat().st_size > MAX_STRUCTURAL_PARSE_BYTES:
                    skipped_large += 1
                    continue
                with open(file_path, encoding="utf-8", errors="ignore") as f:
                    code = f.read()
            except OSError:
                continue

            symbols, _ = parser.parse(file_path, code)
            for symbol in symbols:
                if symbol.name == symbol_name:
                    results.append(
                        {
                            "name": symbol.name,
                            "kind": symbol.kind.value
                            if hasattr(symbol.kind, "value")
                            else symbol.kind,
                            "file": str(file_path),
                            "line": symbol.line_start,
                        }
                    )
            if len(results) >= SYMBOL_SCAN_MAX_RESULTS:
                reasons.append(f"max_results={SYMBOL_SCAN_MAX_RESULTS}")
                break

        if skipped_large:
            reasons.append(f"skipped_files_over_{MAX_STRUCTURAL_PARSE_BYTES}_bytes")
        if reasons:
            results.append(truncation_marker(reasons))
        return results

    async def _search_via_graph(self, symbol_name: str) -> list[dict] | None:
        """Exact-label lookup against the knowledge graph, or ``None`` to fall back.

        Returns ``None`` only when the graph subsystem itself isn't
        available (not yet registered, or the query errors) — the same
        registered-vs-not distinction ``SemanticCodeSearch`` already uses for
        its retrieval fallback. A successful query that simply finds no match
        returns ``[]``, not a fallback to the full scan: consulting the
        graph and finding nothing is a real (and fast) answer, not a
        cold-start signal.
        """
        try:
            container = resolve_container(self._container)
            if not container.has("runtime.knowledge_query"):
                return None
            query = container.get("runtime.knowledge_query")
            if query is None:
                return None
            candidates = await query.find_by_label(symbol_name)
        except Exception:
            return None

        return [
            {
                "name": node.label,
                "kind": str(node.node_type),
                "file": node.file_path,
                "line": node.line_start,
            }
            for node in candidates
            if node.label == symbol_name and str(node.node_type) != "file"
        ]

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Symbol name to search for",
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in",
                },
            },
            "required": ["symbol_name"],
        }
