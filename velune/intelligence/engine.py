"""Repository Intelligence Engine — central coordinator for all repository knowledge.

Responsibilities
----------------
1. **Change detection** — polls the workspace every ``poll_interval`` seconds via
   IncrementalIndexer; uses the git fast-path so unchanged repos cost ~5 ms/tick.

2. **Event emission** — translates IndexDelta into typed ``RepositoryEventType``
   events on the CognitiveBus.  Future features subscribe; they never poll.

3. **Incremental graph updates** — calls KnowledgeGraphPatcher to apply surgical
   node/edge changes rather than a full rebuild.

4. **Git state tracking** — detects branch switches and HEAD SHA changes on its
   own lower-frequency loop and emits ``repository.git_state_changed``.

5. **Downstream scheduling** — a non-blocking task queue drains profile refreshes
   and future embedding/summary updates without touching the CLI event loop.

6. **Lifecycle** — starts and stops cleanly via ``initialize()`` / ``shutdown()``;
   errors in background tasks are logged and never crash the REPL.

Architecture constraints honoured
----------------------------------
* No new event bus — uses the existing CognitiveBus.
* No new file scanner — drives the existing IncrementalIndexer.
* No git logic duplication — uses the existing GitTracker.
* No full graph rebuild — drives KnowledgeGraphPatcher.
* All disk I/O runs in asyncio.to_thread, never blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.events import CognitiveBus
    from velune.knowledge.graph import KnowledgeGraph
    from velune.repository.cognition import RepositoryCognitionService

from velune.intelligence.events import (
    make_engine_started,
    make_engine_stopped,
    make_files_changed,
    make_git_state_changed,
    make_index_updated,
    make_knowledge_graph_patched,
    make_profile_refreshed,
)
from velune.intelligence.graph_patcher import KnowledgeGraphPatcher
from velune.repository.incremental_indexer import IncrementalIndexer, IndexDelta
from velune.repository.tracker import GitTracker

logger = logging.getLogger("velune.intelligence.engine")

# Poll intervals (seconds)
_DEFAULT_CHANGE_POLL = 3.0
_DEFAULT_GIT_POLL = 10.0
# After a change is detected, wait before triggering the next check to avoid
# bursts during large file writes (npm install, code generation, etc.)
_COOLDOWN_AFTER_CHANGE = 8.0
# Maximum pending downstream tasks before we start dropping the lowest priority
_DOWNSTREAM_QUEUE_MAXSIZE = 32


@dataclass
class _DownstreamTask:
    task_type: str  # "graph_patch" | "profile_refresh"
    delta: IndexDelta | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class _GitState:
    branch: str | None = None
    sha: str | None = None
    uncommitted: int = 0


class RepositoryIntelligenceEngine:
    """Central coordinator for all repository knowledge.

    Wires together change detection, event emission, incremental graph
    updates, git state tracking, and downstream scheduling into a single,
    cleanly lifecycle-managed service.

    Lifecycle::

        engine = RepositoryIntelligenceEngine(...)
        await engine.initialize()   # starts background tasks
        ...
        await engine.shutdown()     # drains queue, cancels tasks, emits stopped event
    """

    def __init__(
        self,
        workspace: Path,
        cognition: RepositoryCognitionService,
        knowledge_graph: KnowledgeGraph,
        bus: CognitiveBus,
        *,
        retrieval: Any | None = None,
        change_poll_interval: float = _DEFAULT_CHANGE_POLL,
        git_poll_interval: float = _DEFAULT_GIT_POLL,
    ) -> None:
        self._workspace = workspace.resolve()
        self._cognition = cognition
        self._graph = knowledge_graph
        self._bus = bus
        # Optional: HybridRetriever, used only to purge vector-store entries for
        # files removed since the last index (see _handle_graph_patch). None
        # is a normal, fully-supported state — vector cleanup is then just skipped.
        self._retrieval = retrieval
        self._change_poll = change_poll_interval
        self._git_poll = git_poll_interval

        self._state_path = self._workspace / ".velune" / "index_state.json"
        self._patcher = KnowledgeGraphPatcher(knowledge_graph, self._workspace)
        self._tracker = GitTracker(self._workspace)

        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._downstream_queue: asyncio.Queue[_DownstreamTask] = asyncio.Queue(
            maxsize=_DOWNSTREAM_QUEUE_MAXSIZE
        )
        self._git_state = _GitState()
        self._last_change_at: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Start all background tasks and emit ENGINE_STARTED."""
        if self._running:
            return
        self._running = True

        # Initialize the KnowledgeGraph schema (idempotent)
        try:
            await self._graph.initialize()
        except Exception as exc:
            logger.warning("KnowledgeGraph init failed (non-fatal): %s", exc)

        # Seed git state before loops start so the first diff is accurate. This
        # engine is registered as a Tier-1 lifecycle component (see
        # velune/intelligence/module.py); LifecycleCoordinator.startup() aborts
        # the *entire* app if a registered component's initialize() raises, so
        # a transient git failure (no git binary, detached worktree, ...) here
        # must never escape.
        try:
            self._git_state = await asyncio.to_thread(self._read_git_state)
        except Exception as exc:
            logger.warning("Git state seed failed (non-fatal): %s", exc)

        self._tasks = [
            asyncio.create_task(self._change_detection_loop(), name="rie-change-detection"),
            asyncio.create_task(self._git_state_loop(), name="rie-git-state"),
            asyncio.create_task(self._downstream_worker(), name="rie-downstream"),
        ]

        await self._emit(make_engine_started(str(self._workspace)))
        logger.info("RepositoryIntelligenceEngine started for %s", self._workspace)

    async def shutdown(self) -> None:
        """Stop all background tasks, drain the downstream queue, emit ENGINE_STOPPED."""
        if not self._running:
            return
        self._running = False

        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Drain remaining downstream tasks (best-effort, non-blocking)
        drained = 0
        while not self._downstream_queue.empty():
            try:
                task = self._downstream_queue.get_nowait()
                await self._run_downstream_task(task)
                drained += 1
            except Exception:
                break

        if drained:
            logger.debug("Drained %d downstream tasks on shutdown.", drained)

        await self._emit(make_engine_stopped(str(self._workspace)))
        logger.info("RepositoryIntelligenceEngine stopped.")

    # ------------------------------------------------------------------
    # Background loop: change detection
    # ------------------------------------------------------------------

    async def _change_detection_loop(self) -> None:
        """Poll for file-system changes; emit events and queue downstream work."""
        inc = IncrementalIndexer(self._workspace, self._state_path)

        while self._running:
            try:
                await asyncio.sleep(self._change_poll)

                delta = await inc.compute_delta()
                if delta.is_empty:
                    continue

                self._last_change_at = time.monotonic()
                logger.debug(
                    "RIE change detected: +%d ~%d -%d files",
                    len(delta.to_add),
                    len(delta.to_update),
                    len(delta.to_remove),
                )

                # Emit files_changed immediately so subscribers know what happened
                await self._emit(make_files_changed(delta.to_add, delta.to_update, delta.to_remove))

                # Apply the delta to IndexState (runs in thread pool)
                try:
                    new_state = await inc.apply_delta(delta)
                    await self._emit(make_index_updated(new_state.last_commit_sha))
                except Exception as exc:
                    logger.warning("Index apply_delta failed: %s", exc)

                # Queue downstream work (non-blocking — drop if full)
                self._enqueue_downstream(_DownstreamTask(task_type="graph_patch", delta=delta))
                self._enqueue_downstream(_DownstreamTask(task_type="profile_refresh"))

                # Cool down to avoid burst re-indexing
                await asyncio.sleep(_COOLDOWN_AFTER_CHANGE)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Change detection tick error (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Background loop: git state
    # ------------------------------------------------------------------

    async def _git_state_loop(self) -> None:
        """Detect branch switches and HEAD SHA changes; emit git_state_changed."""
        while self._running:
            try:
                await asyncio.sleep(self._git_poll)

                new_state = await asyncio.to_thread(self._read_git_state)
                changed = self._diff_git_state(new_state)

                if changed:
                    self._git_state = new_state
                    await self._emit(
                        make_git_state_changed(
                            branch=new_state.branch,
                            commit_sha=new_state.sha,
                            uncommitted_files=new_state.uncommitted,
                            changed=changed,
                        )
                    )
                    logger.debug("RIE git state changed: %s", changed)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Git state tick error (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Background loop: downstream task worker
    # ------------------------------------------------------------------

    async def _downstream_worker(self) -> None:
        """Drain the downstream task queue sequentially without blocking the CLI."""
        while self._running:
            try:
                task = await asyncio.wait_for(self._downstream_queue.get(), timeout=5.0)
                await self._run_downstream_task(task)
                self._downstream_queue.task_done()
            except asyncio.TimeoutError:
                continue  # nothing queued — keep looping
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Downstream worker error (non-fatal): %s", exc)

    async def _run_downstream_task(self, task: _DownstreamTask) -> None:
        """Dispatch a downstream task to its handler."""
        try:
            if task.task_type == "graph_patch" and task.delta is not None:
                await self._handle_graph_patch(task.delta)
            elif task.task_type == "profile_refresh":
                await self._handle_profile_refresh()
        except Exception as exc:
            logger.debug("Downstream task '%s' failed (non-fatal): %s", task.task_type, exc)

    # ------------------------------------------------------------------
    # Downstream handlers
    # ------------------------------------------------------------------

    async def _handle_graph_patch(self, delta: IndexDelta) -> None:
        """Apply the delta surgically to the KnowledgeGraph, and purge vector
        entries for any removed files so the vector store never accumulates
        stale embeddings for files that no longer exist."""
        result = await self._patcher.patch(delta)
        await self._emit(
            make_knowledge_graph_patched(
                nodes_added=result.nodes_added,
                nodes_removed=result.nodes_removed,
                edges_added=result.edges_added,
            )
        )

        if self._retrieval is not None and delta.to_remove:
            try:
                await asyncio.to_thread(
                    self._retrieval.vector_retriever.delete_by_ids, delta.to_remove
                )
            except Exception as exc:
                logger.debug("Vector cleanup for removed files failed (non-fatal): %s", exc)

    async def _handle_profile_refresh(self) -> None:
        """Refresh repository metadata and emit profile_refreshed."""
        try:
            profile = await asyncio.to_thread(self._cognition.quick_summary)
            await self._emit(make_profile_refreshed(profile))
        except Exception as exc:
            logger.debug("Profile refresh failed: %s", exc)

    # ------------------------------------------------------------------
    # Git state helpers (synchronous, run in thread)
    # ------------------------------------------------------------------

    def _read_git_state(self) -> _GitState:
        branch = self._tracker.get_active_branch()
        commits = self._tracker.get_recent_commits(limit=1)
        sha = commits[0]["hash"] if commits else None
        uncommitted = len(self._tracker.get_uncommitted_changes())
        return _GitState(branch=branch, sha=sha, uncommitted=uncommitted)

    def _diff_git_state(self, new: _GitState) -> list[str]:
        """Return list of field names that changed between current and new state."""
        changed: list[str] = []
        if new.branch != self._git_state.branch:
            changed.append("branch")
        if new.sha != self._git_state.sha:
            changed.append("sha")
        if new.uncommitted != self._git_state.uncommitted:
            changed.append("uncommitted")
        return changed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _enqueue_downstream(self, task: _DownstreamTask) -> None:
        """Non-blocking enqueue; silently drops if the queue is full."""
        try:
            self._downstream_queue.put_nowait(task)
        except asyncio.QueueFull:
            logger.debug("Downstream queue full; dropping task '%s'.", task.task_type)

    async def _emit(self, event: Any) -> None:
        """Publish an event to the CognitiveBus; swallow errors to stay non-critical."""
        try:
            await self._bus.emit(event)
        except Exception as exc:
            logger.debug("Event emission failed (non-fatal): %s", exc)

    # ------------------------------------------------------------------
    # Status / introspection
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def current_git_state(self) -> dict[str, Any]:
        return {
            "branch": self._git_state.branch,
            "sha": self._git_state.sha,
            "uncommitted": self._git_state.uncommitted,
        }

    @property
    def downstream_queue_size(self) -> int:
        return self._downstream_queue.qsize()
