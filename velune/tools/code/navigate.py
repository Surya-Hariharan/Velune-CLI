from __future__ import annotations

from pathlib import Path
from typing import Optional
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
    ) -> Optional[dict]:
        """Go to symbol definition."""
        from velune.repository.ast.parser import ASTParser
        from velune.repository.ast.extractors.python import PythonSymbolExtractor
        
        path = Path(file_path)
        parser = ASTParser()
        extractor = PythonSymbolExtractor()
        
        ast_tree = parser.parse(path)
        if ast_tree:
            symbols = extractor.extract(ast_tree, str(path))
            for symbol in symbols:
                if symbol.name == symbol_name:
                    return {
                        "file": str(path),
                        "line": symbol.line_start,
                        "kind": symbol.kind,
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
