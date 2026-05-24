from __future__ import annotations
from pathlib import Path
from typing import List
from velune.tools.base.tool import BaseTool


class SemanticCodeSearch(BaseTool):
    """Tool for semantic code search."""

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
        """Search code semantically."""
        # This would use vector search in production
        # For now, do simple keyword search
        from velune.tools.filesystem.search import GrepFiles
        
        grep = GrepFiles()
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
        from velune.repository.scanner import FilesystemScanner
        from velune.repository.parser import ASTParser
        
        root_path = Path(directory)
        scanner = FilesystemScanner(root_path)
        files = scanner.scan([".py"])
        
        parser = ASTParser()
        
        results = []
        for file_path in files:
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    code = f.read()
            except Exception:
                continue
                
            symbols, _ = parser.parse(file_path, code)
            for symbol in symbols:
                if symbol.name == symbol_name:
                    results.append({
                        "name": symbol.name,
                        "kind": symbol.kind.value if hasattr(symbol.kind, "value") else symbol.kind,
                        "file": str(file_path),
                        "line": symbol.line_start,
                    })
        
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
