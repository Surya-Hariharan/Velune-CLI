"""Events → repository re-indexing."""

from typing import Optional
from pathlib import Path
from velune.events.bus.engine import Event
from velune.repository.indexer.incremental import IncrementalIndexer


class IndexEventHandler:
    """Handles events by triggering repository indexing."""

    def __init__(self, incremental_indexer: IncrementalIndexer):
        self.incremental_indexer = incremental_indexer

    async def handle_file_created(self, event: Event) -> None:
        """Handle file created event."""
        file_path = Path(event.data.get("file_path"))
        await self.incremental_indexer.index_new(file_path)

    async def handle_file_modified(self, event: Event) -> None:
        """Handle file modified event."""
        file_path = Path(event.data.get("file_path"))
        await self.incremental_indexer.reindex_changed(file_path)

    async def handle_file_deleted(self, event: Event) -> None:
        """Handle file deleted event."""
        file_path = Path(event.data.get("file_path"))
        await self.incremental_indexer.remove_deleted(file_path)
