"""Repository cognition pipeline merging AST indices, Git history, and dependency graphs."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from velune.repository.analyzer import CodebaseAnalyzer
from velune.repository.grapher import RepositoryGrapher
from velune.repository.indexer import RepositoryIndexer
from velune.repository.schemas import (
    RepositoryEdge,
    RepositorySnapshot,
)
from velune.repository.tracker import GitTracker

logger = logging.getLogger("velune.repository.cognition")

_STATE_FILENAME = "index_state.json"


class RepositoryCognitionService:
    """The unified cognitive entrypoint mapping a workspace's AST structure, Git details, and dependencies."""

    def __init__(self, root_path: Path) -> None:
        self.root_path = root_path.resolve()
        self.indexer = RepositoryIndexer(self.root_path)
        self.grapher = RepositoryGrapher(self.root_path)
        self.tracker = GitTracker(self.root_path)
        self.analyzer = CodebaseAnalyzer(self.root_path)
        self._state_path = self.root_path / ".velune" / _STATE_FILENAME
        self._bg_index_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle protocol (called by LifecycleCoordinator)
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Start background incremental indexing — non-blocking for the REPL."""
        from velune.repository.incremental_indexer import IncrementalIndexer

        try:
            from rich.console import Console

            console = Console(stderr=True)
        except Exception:
            console = None  # type: ignore[assignment]

        def _print(msg: str) -> None:
            if console:
                console.print(msg)

        _print("[dim]Checking repository...[/dim]")

        inc = IncrementalIndexer(self.workspace_root, self._state_path)
        try:
            delta = await inc.compute_delta()
        except Exception as exc:
            logger.warning("Incremental delta check failed: %s", exc)
            return

        if delta.is_empty:
            _print("[dim green]Repository index is up to date[/dim green]")
            return

        n = delta.total
        _print(f"[dim]Indexing {n} changed file(s)...[/dim]")
        self._bg_index_task = asyncio.create_task(self._background_apply(inc, delta, n))

    async def shutdown(self) -> None:
        """Cancel any pending background indexing task."""
        if self._bg_index_task and not self._bg_index_task.done():
            self._bg_index_task.cancel()
            try:
                await self._bg_index_task
            except (asyncio.CancelledError, Exception):
                pass

    # ------------------------------------------------------------------
    # Primary indexing API
    # ------------------------------------------------------------------

    @property
    def workspace_root(self) -> Path:
        return self.root_path

    def index(self, force: bool = False) -> RepositorySnapshot:
        """Index the repository, using a git-SHA fast path to skip unchanged repos.

        On a cache hit (git HEAD SHA matches stored SHA AND working tree is clean),
        the existing symbol + file cache is loaded and the full pipeline (grapher,
        git metrics, architecture analysis) is run on top of it — skipping all file
        I/O and SHA computation.

        On a cache miss, the full ``RepositoryIndexer`` pipeline runs (incremental
        at the file level via SHA256 comparison) and the ``IndexState`` is updated.
        """
        from velune.repository.incremental_indexer import IncrementalIndexer

        inc = IncrementalIndexer(self.root_path, self._state_path)

        if not force and inc._get_git_sha() == self._stored_commit_sha():
            clean = inc._working_tree_is_clean()
            if clean:
                cached = self.get_snapshot()
                if cached:
                    logger.debug("Fast path: reusing cached snapshot (git SHA matches).")
                    return self._run_pipeline(cached, update_state=False)

        # Slow path: file-level incremental index
        snapshot = self.indexer.index(force=force)

        # Persist updated IndexState so the next session benefits from the fast path
        self._persist_index_state(inc, snapshot)

        return self._run_pipeline(snapshot, update_state=False)

    # ------------------------------------------------------------------
    # Read-only snapshot accessor (no indexing)
    # ------------------------------------------------------------------

    def get_snapshot(self) -> RepositorySnapshot | None:
        """Return the last-computed snapshot from the on-disk cache, or None."""
        import json

        cache_path = self.indexer.cache_path
        if not cache_path.exists():
            return None

        try:
            with open(cache_path, encoding="utf-8") as f:
                cache: dict = json.load(f)
        except Exception:
            return None

        from velune.repository.schemas import (
            RepositoryFile,
            RepositoryLanguage,
            RepositorySymbol,
        )

        files: list[RepositoryFile] = []
        all_symbols: list[RepositorySymbol] = []

        for rel_path, entry in cache.items():
            try:
                language = RepositoryLanguage(entry.get("language", "unknown"))
                symbols = [RepositorySymbol(**s) for s in entry.get("symbols", [])]
                file_rec = RepositoryFile(
                    path=rel_path,
                    language=language,
                    size_bytes=entry.get("size_bytes", 0),
                    sha256=entry.get("sha256", ""),
                    symbols=symbols,
                    metadata=entry.get("metadata", {}),
                )
                files.append(file_rec)
                all_symbols.extend(symbols)
            except Exception:
                continue

        return RepositorySnapshot(
            root_path=str(self.root_path),
            files=files,
            symbols=all_symbols,
            edges=[],
            summary={
                "total_files": len(files),
                "total_symbols": len(all_symbols),
            },
        )

    def traverse(self, node_id: str, depth: int = 2) -> list[str]:
        """BFS traversal from *node_id* through the dependency graph."""
        return self.grapher.traverse(node_id, depth)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_pipeline(
        self, snapshot: RepositorySnapshot, *, update_state: bool = False
    ) -> RepositorySnapshot:
        """Run grapher, git metrics, and architecture analysis on *snapshot*."""
        # Reset grapher for this run (it is stateful and must be rebuilt each call)
        self.grapher = RepositoryGrapher(self.root_path)

        file_paths = [f.path for f in snapshot.files]
        for f in snapshot.files:
            self.grapher.add_file(f.path, f.language.value, f.size_bytes)
            for sym in f.symbols:
                self.grapher.add_symbol(sym)

        self.grapher.resolve_import_dependencies(file_paths, snapshot.symbols)

        edges: list[RepositoryEdge] = []
        for src, tgt, _key, data in self.grapher.graph.edges(keys=True, data=True):
            edges.append(
                RepositoryEdge(
                    source=src,
                    target=tgt,
                    edge_type=data.get("edge_type", "depends"),
                    weight=data.get("weight", 1.0),
                )
            )
        snapshot.edges = edges

        # Git metrics (batched subprocesses)
        branch = self.tracker.get_active_branch()
        changes = self.tracker.get_uncommitted_changes()
        recent_commits = self.tracker.get_recent_commits(limit=5)
        all_volatility = self.tracker.get_all_file_volatility(days=90)
        file_volatility: dict[str, int] = {}
        for f in snapshot.files:
            file_volatility[f.path] = all_volatility.get(f.path, 0) or all_volatility.get(
                f.path.replace("/", "\\"), 0
            )

        # Architecture analysis (pure Python)
        layers = self.analyzer.classify_architecture_layers(file_paths)
        analyzer_edges = [(e.source, e.target) for e in edges]
        violations = self.analyzer.detect_dependency_violations(layers, analyzer_edges)

        code_files: dict[str, str] = {}
        for f in snapshot.files:
            if f.size_bytes < 100_000:
                try:
                    code_files[f.path] = (self.root_path / f.path).read_text(
                        encoding="utf-8", errors="ignore"
                    )
                except Exception:
                    pass
        frameworks = self.analyzer.detect_framework_footprint(code_files)

        snapshot.summary.update(
            {
                "git": {
                    "active_branch": branch,
                    "uncommitted_changes_count": len(changes),
                    "uncommitted_changes": changes[:10],
                    "recent_commits": recent_commits,
                },
                "architecture": {
                    "layers": {k: len(v) for k, v in layers.items()},
                    "violations_count": len(violations),
                    "violations": violations[:5],
                    "frameworks_detected": frameworks,
                },
                "metrics": {
                    "high_volatility_files": sorted(
                        file_volatility.items(), key=lambda x: x[1], reverse=True
                    )[:5],
                },
            }
        )

        return snapshot

    def _stored_commit_sha(self) -> str | None:
        """Return the git SHA stored in the last saved IndexState."""
        from velune.repository.index_state import IndexState

        state = IndexState.load(self._state_path)
        return state.last_commit_sha if state else None

    def _persist_index_state(self, inc: object, snapshot: RepositorySnapshot) -> None:
        """Update IndexState on disk after a full index run."""
        import time

        from velune.repository.index_state import IndexedFile, IndexState

        try:
            git_sha = inc._get_git_sha()  # type: ignore[attr-defined]
            state = IndexState.load(self._state_path) or IndexState.empty(str(self.root_path))
            now = time.time()
            state.file_index = {
                f.path: IndexedFile(
                    path=f.path,
                    content_hash=f.sha256,
                    language=f.language.value,
                    symbol_count=len(f.symbols),
                    indexed_at=now,
                )
                for f in snapshot.files
            }
            state.touch(git_sha)
            state.workspace_root = str(self.root_path)
            state.save(self._state_path)
        except Exception as exc:
            logger.debug("Could not persist IndexState: %s", exc)

    async def _background_apply(
        self,
        inc: object,
        delta: object,
        n: int,
    ) -> None:
        """Apply a delta in the background and announce completion."""
        try:
            from rich.console import Console

            console = Console(stderr=True)
        except Exception:
            console = None  # type: ignore[assignment]

        try:
            await inc.apply_delta(delta)  # type: ignore[attr-defined]
            msg = f"[dim green]Repository index updated ({n} file(s))[/dim green]"
            if console:
                console.print(msg)
            logger.info("Background incremental index complete: %d file(s) processed.", n)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Background incremental indexing failed: %s", exc)
