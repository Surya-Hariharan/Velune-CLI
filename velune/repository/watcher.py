"""WorkspaceWatcher — background asyncio task that detects workspace changes.

The incremental indexer already knows how to detect file-level changes cheaply
(git SHA fast path → if dirty, SHA-256 per file).  What was missing is a driver
that calls it continuously so the orchestrator always has a fresh snapshot when
a user submits a new prompt.

Architecture
------------
WorkspaceWatcher runs as a low-frequency asyncio task (default: every 3 s).
Each tick it calls ``RepositoryCognitionService.probe_for_changes()``, which:

1. Runs ``git diff HEAD --name-only`` (< 5 ms, no I/O beyond a subprocess).
2. If the tree is clean *and* the HEAD SHA matches the stored SHA → returns
   immediately (no file reads).
3. If dirty, computes per-file SHA-256 deltas (I/O proportional to changed
   files only) and fires ``apply_delta`` in a sub-task.

The watcher therefore costs ~5 ms per tick on an unchanged repo and is bounded
to the number of changed files on a dirty tree — safe to run continuously in
the background alongside the REPL event loop.

Lifecycle
---------
    watcher = WorkspaceWatcher(cognition_service)
    await watcher.start()   # called by LifecycleCoordinator / runtime init
    ...
    await watcher.stop()    # called on REPL exit / shutdown
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.repository.cognition import RepositoryCognitionService

logger = logging.getLogger("velune.repository.watcher")

# How long to wait between dirty-tree checks.  Shorter = more responsive; longer =
# fewer background wakeups.  3 s is a good balance for interactive use.
_DEFAULT_POLL_INTERVAL = 3.0

# After a re-index is triggered, wait this long before the next check so we don't
# pile up tasks during a heavy file-write burst (e.g. npm install, code generation).
_COOLDOWN_AFTER_CHANGE = 10.0


class WorkspaceWatcher:
    """Polls the workspace for changes and triggers incremental re-indexing.

    This is intentionally *not* an inotify / watchdog / fsevents watcher —
    those require OS-level APIs, platform-specific packages, and permission
    grants that Velune does not want to mandate.  The git-based polling
    approach is portable, low-overhead, and already matches the indexer's
    own change-detection mechanism.
    """

    def __init__(
        self,
        cognition: RepositoryCognitionService,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
    ) -> None:
        self._cognition = cognition
        self._poll_interval = poll_interval
        self._task: asyncio.Task | None = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background watcher task."""
        if self._task and not self._task.done():
            return  # already running
        self._running = True
        self._task = asyncio.create_task(self._watch_loop(), name="workspace-watcher")
        logger.debug("WorkspaceWatcher started (poll interval: %.1fs)", self._poll_interval)

    async def stop(self) -> None:
        """Stop the watcher and await its cancellation."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.debug("WorkspaceWatcher stopped.")

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _watch_loop(self) -> None:
        """Poll indefinitely; trigger re-index when changes are detected."""
        while self._running:
            try:
                await asyncio.sleep(self._poll_interval)
                changed = await self._cognition.probe_for_changes()
                if changed:
                    logger.debug(
                        "WorkspaceWatcher: workspace changed — re-index triggered; "
                        "cooling down for %.1fs.",
                        _COOLDOWN_AFTER_CHANGE,
                    )
                    await asyncio.sleep(_COOLDOWN_AFTER_CHANGE)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never let the watcher crash the REPL — log and keep going.
                logger.debug("WorkspaceWatcher tick error (non-fatal): %s", exc)
