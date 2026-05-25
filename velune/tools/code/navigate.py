from __future__ import annotations

from pathlib import Path

from velune.tools.base.tool import BaseTool


class GoToDefinition(BaseTool):
    """Tool for navigating to symbol definitions."""

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
        """Go to symbol definition."""
        from velune.repository.parser import ASTParser

        path = Path(file_path)
        parser = ASTParser()

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

    def get_name(self) -> str:
        return "find_references"

    def get_description(self) -> str:
        return "Find all references to a symbol"

    async def execute(
        self,
        symbol_name: str,
        directory: str = ".",
    ) -> list[dict]:
        """Find references to a symbol."""
        from velune.tools.filesystem.search import GrepFiles

        grep = GrepFiles()
        results = await grep.execute(
            pattern=symbol_name,
            directory=directory,
            file_pattern="*.py",
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
                "directory": {
                    "type": "string",
                    "description": "Directory to search in",
                },
            },
            "required": ["symbol_name"],
        }
