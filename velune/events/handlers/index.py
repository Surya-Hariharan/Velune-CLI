"""Events → repository re-indexing."""

from typing import Optional
from pathlib import Path
from velune.events.bus.engine import Event
from velune.repository.indexer import RepositoryIndexer


class IndexEventHandler:
    """Handles events by triggering repository indexing."""

    def __init__(self, indexer: RepositoryIndexer):
        self.indexer = indexer

    async def handle_file_created(self, event: Event) -> None:
        """Handle file created event."""
        self.indexer.index(force=False)

    async def handle_file_modified(self, event: Event) -> None:
        """Handle file modified event."""
        self.indexer.index(force=False)

    async def handle_file_deleted(self, event: Event) -> None:
        """Handle file deleted event."""
        self.indexer.index(force=True)

