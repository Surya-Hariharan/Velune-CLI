"""Repository cognitive model."""

from pathlib import Path
from typing import Dict, list
from datetime import datetime
from velune.core.types import FileNode, SymbolNode


class RepositoryCognitiveModel:
    """Cognitive model of the repository."""

    def __init__(self, root_path: Path):
        self.root_path = root_path
        self.files: Dict[str, FileNode] = {}
        self.symbols: Dict[str, list[SymbolNode]] = {}
        self.last_updated: Optional[datetime] = None

    def add_file(self, file_node: FileNode) -> None:
        """Add a file to the model."""
        self.files[file_node.path] = file_node
        self.last_updated = datetime.now()

    def add_symbols(self, file_path: str, symbols: list[SymbolNode]) -> None:
        """Add symbols for a file."""
        self.symbols[file_path] = symbols
        self.last_updated = datetime.now()

    def get_file(self, file_path: str) -> Optional[FileNode]:
        """Get a file node."""
        return self.files.get(file_path)

    def get_symbols(self, file_path: str) -> list[SymbolNode]:
        """Get symbols for a file."""
        return self.symbols.get(file_path, [])

    def find_symbol(self, symbol_name: str) -> list[SymbolNode]:
        """Find a symbol by name across all files."""
        results = []
        for symbols in self.symbols.values():
            for symbol in symbols:
                if symbol.name == symbol_name:
                    results.append(symbol)
        return results

    def get_statistics(self) -> Dict[str, any]:
        """Get repository statistics."""
        return {
            "file_count": len(self.files),
            "symbol_count": sum(len(s) for s in self.symbols.values()),
            "last_updated": self.last_updated,
        }
