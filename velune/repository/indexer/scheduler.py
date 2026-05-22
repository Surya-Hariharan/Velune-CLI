"""Background indexing scheduler."""

import asyncio
from pathlib import Path
from typing import Optional
from velune.repository.indexer.incremental import IncrementalIndexer
from velune.repository.scanner.watcher import FileWatcher


class IndexingScheduler:
    """Schedules background indexing."""

    def __init__(self, root_path: Path, incremental_indexer: IncrementalIndexer):
        self.root_path = root_path
        self.incremental_indexer = incremental_indexer
        self.watcher = FileWatcher(root_path)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the indexing scheduler."""
        if self._running:
            return
        
        self._running = True
        
        # Set up file watcher callbacks
        self.watcher.add_callback(self._on_file_change)
        self.watcher.start()
        
        # Start background task
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the indexing scheduler."""
        self._running = False
        self.watcher.stop()
        
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        """Run the scheduler loop."""
        while self._running:
            await asyncio.sleep(60)  # Check every minute

    async def _on_file_change(self, file_path: Path, event_type: str) -> None:
        """Handle file change events."""
        if event_type == "created" or event_type == "modified":
            if not self.incremental_indexer.is_indexed(file_path):
                await self.incremental_indexer.index_new(file_path)
            else:
                await self.incremental_indexer.reindex_changed(file_path)
        elif event_type == "deleted":
            await self.incremental_indexer.remove_deleted(file_path)
