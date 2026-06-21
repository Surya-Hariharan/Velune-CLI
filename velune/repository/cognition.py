"""Repository cognition pipeline merging AST indices, Git history, and dependency graphs."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from velune.repository.analyzer import CodebaseAnalyzer
from velune.repository.architecture_detector import ArchitectureDetector
from velune.repository.grapher import RepositoryGrapher
from velune.repository.indexer import RepositoryIndexer
from velune.repository.schemas import (
    RepositoryEdge,
    RepositorySnapshot,
)
from velune.repository.technology_detector import TechnologyDetector
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
        # Tracks the most recent file-level change set so the orchestrator can
        # surface "what changed since last run" in the prompt without re-indexing.
        self._last_delta: object | None = None  # IndexDelta | None

    @property
    def last_delta(self) -> object | None:
        """The IndexDelta from the most recent incremental index, or None."""
        return self._last_delta

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
        self._last_delta = delta
        self._bg_index_task = asyncio.create_task(self._background_apply(inc, delta, n))

    async def probe_for_changes(self) -> bool:
        """Check if the workspace has changed since the last index; re-index if so.

        Returns True when an incremental re-index was triggered, False when the
        workspace was already up to date.  Safe to call frequently — the git-SHA
        fast path makes it cheap when nothing has changed.
        """
        from velune.repository.incremental_indexer import IncrementalIndexer

        inc = IncrementalIndexer(self.workspace_root, self._state_path)
        try:
            delta = await inc.compute_delta()
        except Exception as exc:
            logger.debug("probe_for_changes: delta computation failed: %s", exc)
            return False

        if delta.is_empty:
            return False

        logger.debug(
            "probe_for_changes: %d file(s) changed — triggering incremental re-index.",
            delta.total,
        )
        self._last_delta = delta
        # Re-use the background task slot so there is at most one running at a time.
        if self._bg_index_task and not self._bg_index_task.done():
            self._bg_index_task.cancel()
            try:
                await self._bg_index_task
            except (asyncio.CancelledError, Exception):
                pass
        self._bg_index_task = asyncio.create_task(self._background_apply(inc, delta, delta.total))
        return True

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
        """Run grapher, git metrics, architecture analysis, and API mapping on *snapshot*."""
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

        # Architecture analysis — adaptive to the actual project type
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

        # API connection map — frontend ↔ backend ↔ database chain
        api_map_data: dict = {}
        try:
            from velune.repository.api_mapper import APIMapper, render_api_map

            api_mapper = APIMapper(self.root_path)
            api_map = api_mapper.build_map(file_paths)
            snapshot.api_map = api_map  # type: ignore[attr-defined]
            api_map_data = {
                "route_count": len(api_map.routes),
                "frontend_call_count": len(api_map.frontend_calls),
                "db_query_count": len(api_map.db_queries),
                "connection_count": len(api_map.connections),
                "api_map_text": render_api_map(api_map, max_tokens=1500),
            }
            if api_map.routes:
                logger.info(
                    "API map: %d routes, %d frontend calls, %d db ops, %d connections",
                    len(api_map.routes),
                    len(api_map.frontend_calls),
                    len(api_map.db_queries),
                    len(api_map.connections),
                )
        except Exception as exc:
            logger.debug("API mapping failed (non-fatal): %s", exc)

        # Technology + Architecture detection
        tech_detector = TechnologyDetector(self.root_path)
        tech_stack = tech_detector.detect()

        arch_detector = ArchitectureDetector(self.root_path, snapshot.files, tech_stack)
        arch_report = arch_detector.detect()

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
                    "layer_membership": dict(layers.items()),
                    "violations_count": len(violations),
                    "violations": violations[:5],
                    "frameworks_detected": frameworks,
                    "project_types": list(self.analyzer.detected_project_types),
                    # New: rich technology + architecture summary
                    "tech_stack": tech_stack.to_dict(),
                    "arch_report": arch_report.to_dict(),
                },
                "metrics": {
                    "high_volatility_files": sorted(
                        file_volatility.items(), key=lambda x: x[1], reverse=True
                    )[:5],
                },
                "api_map": api_map_data,
            }
        )

        # Persist retrieval documents so BM25 survives across sessions
        self._save_retrieval_index(snapshot)

        return snapshot

    def _save_retrieval_index(self, snapshot: RepositorySnapshot) -> None:
        """Write a flat list of retrieval documents derived from the snapshot to disk."""
        docs: list[dict] = []
        for f in snapshot.files:
            # Build a rich content string for BM25
            parts: list[str] = [f.path.split("/")[-1]]  # filename first (high weight)

            non_import_symbols = [s.name for s in f.symbols if s.kind.value != "import"]
            if non_import_symbols:
                parts.append(" ".join(non_import_symbols))

            import_targets = [s.name for s in f.symbols if s.kind.value == "import"]
            if import_targets:
                parts.append(" ".join(import_targets))

            parts.append(f.language.value)
            parts.append(f.path)

            content = " ".join(parts)

            docs.append(
                {
                    "id": f.path,
                    "content": content,
                    "metadata": {
                        "path": f.path,
                        "language": f.language.value,
                        "symbols": [s.name for s in f.symbols[:20]],
                        "size_bytes": f.size_bytes,
                    },
                }
            )

        retrieval_path = self.root_path / ".velune" / "retrieval_index.json"
        try:
            retrieval_path.parent.mkdir(parents=True, exist_ok=True)
            with open(retrieval_path, "w", encoding="utf-8") as fh:
                json.dump(docs, fh)
            logger.debug("Saved %d retrieval documents to %s", len(docs), retrieval_path)
        except Exception as exc:
            logger.debug("Could not save retrieval index: %s", exc)

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
        """Apply a delta in the background with a Rich progress bar on stderr."""
        try:
            from rich.console import Console
            from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

            _console = Console(stderr=True)
            with Progress(
                SpinnerColumn(),
                TextColumn("[dim]{task.description}[/dim]"),
                BarColumn(bar_width=30),
                TextColumn("[dim]{task.completed}/{task.total}[/dim]"),
                console=_console,
                transient=True,
            ) as _prog:
                _task = _prog.add_task("Indexing...", total=n)

                def _cb(processed: int, total: int, rel_path: str) -> None:
                    short = rel_path.rsplit("/", 1)[-1]
                    _prog.update(_task, completed=processed, description=f"Indexing {short}")

                inc.progress_callback = _cb  # type: ignore[attr-defined]
                await inc.apply_delta(delta)  # type: ignore[attr-defined]

            _console.print(f"[dim green]Repository index updated ({n} file(s))[/dim green]")
            logger.info("Background incremental index complete: %d file(s) processed.", n)
            # Keep last_delta so the next orchestrator run can surface it in context,
            # then clear it so subsequent runs don't re-announce the same changes.
            # (The orchestrator reads last_delta before calling index(), so the order is safe.)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Background incremental indexing failed: %s", exc)
