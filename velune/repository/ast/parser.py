"""Tree-sitter multi-language parser."""

from pathlib import Path
from typing import Optional, Any
import tree_sitter


class ASTParser:
    """Multi-language AST parser using tree-sitter."""

    def __init__(self):
        self.parsers = {}
        self._initialize_parsers()

    def _initialize_parsers(self) -> None:
        """Initialize language parsers."""
        try:
            # Python
            from tree_sitter import Language
            py_language = Language("build/python.so", "python")
            self.parsers["python"] = py_language
        except Exception:
            pass

    def parse(self, file_path: Path, code: Optional[str] = None) -> Optional[Any]:
        """Parse a file and return its AST."""
        if code is None:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
        
        language = self._detect_language(file_path)
        if language not in self.parsers:
            return None
        
        parser = tree_sitter.Parser()
        parser.set_language(self.parsers[language])
        
        tree = parser.parse(bytes(code, "utf8"))
        return tree

    def _detect_language(self, file_path: Path) -> str:
        """Detect programming language from file extension."""
        suffix_map = {
            ".py": "python",
            ".js": "javascript",
            ".ts": "typescript",
            ".rs": "rust",
            ".go": "go",
        }
        return suffix_map.get(file_path.suffix, "unknown")
