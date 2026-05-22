"""Filesystem search tools."""

from pathlib import Path
from typing import list
from velune.tools.base.tool import BaseTool


class GrepFiles(BaseTool):
    """Tool for searching file contents."""

    def get_name(self) -> str:
        return "grep_files"

    def get_description(self) -> str:
        return "Search for text in files"

    async def execute(
        self,
        pattern: str,
        directory: str = ".",
        file_pattern: str = "*",
    ) -> list[dict]:
        """Search for pattern in files."""
        import re
        from velune.repository.scanner.filesystem import FilesystemScanner
        
        root_path = Path(directory)
        scanner = FilesystemScanner(root_path)
        files = scanner.scan()
        
        results = []
        regex = re.compile(pattern, re.IGNORECASE)
        
        for file_path in files:
            if not file_path.match(file_pattern):
                continue
            
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                
                matches = regex.finditer(content)
                for match in matches:
                    results.append({
                        "file": str(file_path),
                        "match": match.group(),
                        "line": content[:match.start()].count("\n") + 1,
                    })
            except Exception:
                pass
        
        return results

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Pattern to search for",
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in",
                },
                "file_pattern": {
                    "type": "string",
                    "description": "File pattern to match",
                },
            },
            "required": ["pattern"],
        }


class FindFiles(BaseTool):
    """Tool for finding files by name."""

    def get_name(self) -> str:
        return "find_files"

    def get_description(self) -> str:
        return "Find files by name pattern"

    async def execute(
        self,
        pattern: str,
        directory: str = ".",
    ) -> list[str]:
        """Find files by pattern."""
        import fnmatch
        from pathlib import Path
        
        root_path = Path(directory)
        matches = []
        
        for file_path in root_path.rglob("*"):
            if file_path.is_file() and fnmatch.fnmatch(file_path.name, pattern):
                matches.append(str(file_path))
        
        return matches

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "File name pattern to match",
                },
                "directory": {
                    "type": "string",
                    "description": "Directory to search in",
                },
            },
            "required": ["pattern"],
        }
