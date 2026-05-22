"""Incremental re-indexing on file changes."""

from pathlib import Path
from typing import set
from velune.repository.indexer.pipeline import RepositoryIndexer


class IncrementalIndexer:
    """Incremental indexer for file changes."""

    def __init__(self, indexer: RepositoryIndexer):
        self.indexer = indexer
        self.indexed_files: set[str] = set()

    async def index_new(self, file_path: Path) -> None:
        """Index a new file."""
        await self.indexer.index_file(file_path)
        self.indexed_files.add(str(file_path))

    async def reindex_changed(self, file_path: Path) -> None:
        """Re-index a changed file."""
        await self.indexer.index_file(file_path)
        self.indexed_files.add(str(file_path))

    async def remove_deleted(self, file_path: Path) -> None:
        """Handle a deleted file."""
        file_str = str(file_path)
        if file_str in self.indexed_files:
            self.indexed_files.remove(file_str)
            # Remove from graph store
            # Implementation depends on graph store API

    def is_indexed(self, file_path: Path) -> bool:
        """Check if a file is indexed."""
        return str(file_path) in self.indexed_files
