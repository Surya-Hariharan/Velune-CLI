from __future__ import annotations

from pathlib import Path
from typing import Any

from velune.execution.path_guard import PathGuard
from velune.tools.base.tool import BaseTool


class SemanticCodeSearch(BaseTool):
    """Tool for semantic code search."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

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
            from velune.kernel.registry import get_container

            container = get_container()
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
        results = await grep.execute(pattern=query, directory=directory)
        return results[:limit]

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

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "symbol_search"

    def get_description(self) -> str:
        return "Search for symbols in code"

    async def execute(
        self,
        symbol_name: str,
        directory: str = ".",
    ) -> list[dict]:
        """Search for symbols."""
        from velune.repository.parser import RepositorySnapshotParser
        from velune.repository.scanner import FilesystemScanner

        guard = PathGuard(self.workspace)
        root_path = guard.validate(directory)

        scanner = FilesystemScanner(root_path)
        files = scanner.scan([".py"])

        parser = RepositorySnapshotParser()

        results = []
        for file_path in files:
            try:
                with open(file_path, encoding="utf-8", errors="ignore") as f:
                    code = f.read()
            except Exception:
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

        return results

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
