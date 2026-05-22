"""Semantic repository navigation."""

from pathlib import Path
from typing import list, Optional
from velune.repository.cognition.model import RepositoryCognitiveModel
from velune.core.types import SymbolNode


class RepositoryNavigator:
    """Navigate the repository semantically."""

    def __init__(self, model: RepositoryCognitiveModel):
        self.model = model

    def find_definition(self, symbol_name: str) -> Optional[SymbolNode]:
        """Find the definition of a symbol."""
        symbols = self.model.find_symbol(symbol_name)
        return symbols[0] if symbols else None

    def find_references(self, symbol_name: str) -> list[str]:
        """Find references to a symbol."""
        # This would require cross-reference analysis
        # For now, return the definition location
        symbol = self.find_definition(symbol_name)
        if symbol:
            return [symbol.file_path]
        return []

    def navigate_to_related(self, file_path: str) -> list[str]:
        """Navigate to related files."""
        # This would use the dependency graph
        # For now, return files in the same directory
        path = Path(file_path)
        same_dir_files = [
            str(f) for f in path.parent.iterdir()
            if f.is_file() and str(f) in self.model.files
        ]
        return same_dir_files
