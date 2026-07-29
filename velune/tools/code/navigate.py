from __future__ import annotations

from pathlib import Path
from typing import Any

from velune.execution.path_guard import PathGuard
from velune.tools.base.tool import BaseTool

# Node types that can be a symbol's definition site — excludes NodeType.FILE.
_DEFINITION_NODE_TYPES = frozenset({"class", "function", "method"})


class GoToDefinition(BaseTool):
    """Tool for navigating to symbol definitions."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "go_to_definition"

    def get_description(self) -> str:
        return "Navigate to symbol definition"

    async def execute(
        self,
        symbol_name: str,
        file_path: str,
        line: int,
    ) -> dict | None:
        """Go to symbol definition.

        Tries the repository knowledge graph first — it indexes every file,
        so it can resolve a symbol defined in a *different* file than the
        reference (e.g. an imported name), which a single-file re-parse
        structurally cannot. Falls back to parsing just ``file_path`` when
        the graph isn't available/warm yet or has no match, so this tool
        still works during Tier-1 warm-up or in an unindexed workspace.
        """
        guard = PathGuard(self.workspace)
        path = guard.validate(file_path)

        graph_hit = await self._lookup_via_graph(symbol_name, path)
        if graph_hit is not None:
            return graph_hit

        from velune.repository.parser import RepositorySnapshotParser

        parser = RepositorySnapshotParser()

        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                code = f.read()
        except Exception:
            return None

        symbols, _ = parser.parse(path, code)
        for symbol in symbols:
            if symbol.name == symbol_name:
                return {
                    "file": str(path),
                    "line": symbol.line_start,
                    "kind": symbol.kind.value if hasattr(symbol.kind, "value") else symbol.kind,
                }

        return None

    async def _lookup_via_graph(self, symbol_name: str, ref_path: Path) -> dict[str, Any] | None:
        try:
            from velune.kernel.registry import get_container

            container = get_container()
            if not container.has("runtime.knowledge_query"):
                return None
            query = container.get("runtime.knowledge_query")
            if query is None:
                return None
            candidates = await query.find_by_label(symbol_name)
        except Exception:
            return None

        matches = [
            n
            for n in candidates
            if n.label == symbol_name and str(n.node_type) in _DEFINITION_NODE_TYPES
        ]
        if not matches:
            return None

        # Prefer a definition in the referencing file itself (shadowing,
        # nested defs) over one elsewhere; otherwise take the first match —
        # ``find_by_label`` already bounds the candidate set to 50 rows.
        try:
            ref_rel = str(ref_path.relative_to(self.workspace))
        except ValueError:
            ref_rel = str(ref_path)
        same_file = next((n for n in matches if n.file_path == ref_rel), None)
        chosen = same_file or matches[0]
        return {
            "file": chosen.file_path or ref_rel,
            "line": chosen.line_start,
            "kind": str(chosen.node_type),
        }

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Symbol name",
                },
                "file_path": {
                    "type": "string",
                    "description": "File containing the symbol reference",
                },
                "line": {
                    "type": "integer",
                    "description": "Line number of reference",
                },
            },
            "required": ["symbol_name", "file_path", "line"],
        }


class FindReferences(BaseTool):
    """Tool for finding symbol references."""

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "find_references"

    def get_description(self) -> str:
        return "Find all references to a symbol"

    async def execute(
        self,
        symbol_name: str,
        directory: str = ".",
        file_pattern: str = "*",
    ) -> list[dict]:
        """Find references to a symbol via a grep-backed text search.

        The knowledge graph doesn't model call/reference edges yet (only
        imports and containment — see ``knowledge/schemas.py:EdgeType``), so
        this can't be graph-backed today. ``file_pattern`` used to be
        hardcoded to ``"*.py"``, which silently returned zero references in
        any non-Python repository; it now defaults to every file and can
        still be narrowed by the caller when useful.
        """
        from velune.tools.filesystem.search import GrepFiles

        grep = GrepFiles(workspace=self.workspace)
        results = await grep.execute(
            pattern=symbol_name,
            directory=directory,
            file_pattern=file_pattern,
        )

        return results

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "symbol_name": {
                    "type": "string",
                    "description": "Symbol name to find references for",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "Glob to restrict the search to (e.g. '*.ts'); "
                    "defaults to all files",
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in",
                },
            },
            "required": ["symbol_name"],
        }
