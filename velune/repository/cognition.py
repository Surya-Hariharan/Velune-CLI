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
        """Lifecycle hook — intentionally inert.

        Repository cognition is *never* triggered automatically at startup. It
        is an explicit, user-driven workflow (the ``/cognition`` command). This
        keeps launch instant and avoids scanning unrelated directories when
        Velune is started outside a project. The methods below
        (:meth:`quick_summary`, :meth:`preview`, :meth:`run_incremental`,
        :meth:`index`) are the manual entry points the REPL calls on demand.
        """
        logger.debug("RepositoryCognitionService.initialize: no-op (cognition is manual).")
        return

    async def probe_for_changes(self) -> bool:
        """Check if the workspace has changed since the last index; re-index if so.

        Returns True when an incremental re-index was triggered, False when the
        workspace was already up to date.  Safe to call frequently — the git-SHA
        fast path makes it cheap when nothing has changed.
        """
        from velune.repository.incremental_indexer import IncrementalIndexer
        from velune.repository.scanner import unsafe_index_root_reason

        if unsafe_index_root_reason(self.workspace_root):
            return False

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

    # ------------------------------------------------------------------
    # Manual cognition entry points (called by the /cognition command)
    # ------------------------------------------------------------------

    def unsafe_reason(self) -> str | None:
        """Return a human-readable reason this root must not be indexed, or None."""
        from velune.repository.scanner import unsafe_index_root_reason

        return unsafe_index_root_reason(self.workspace_root)

    def quick_summary(self) -> dict:
        """Fast, manifest-only scan: technology stack + project type. No indexing.

        Targets a 2–5s budget by reading only well-known manifest files
        (pyproject.toml, package.json, Cargo.toml, …) via the existing
        detectors rather than walking the whole tree.
        """
        from velune.repository.project_type import ProjectTypeDetector
        from velune.repository.technology_detector import TechnologyDetector

        summary: dict = {"root": str(self.root_path)}
        try:
            tech = TechnologyDetector(self.root_path).detect()
            summary["tech_stack"] = tech.to_dict()
        except Exception as exc:
            logger.debug("quick_summary: technology detection failed: %s", exc)
        try:
            profile = ProjectTypeDetector().detect(self.root_path)
            if profile is not None:
                summary["project_type"] = (
                    profile.get("display_name")
                    if isinstance(profile, dict)
                    else getattr(profile, "display_name", None)
                )
        except Exception as exc:
            logger.debug("quick_summary: project-type detection failed: %s", exc)
        return summary

    async def preview(self) -> dict:
        """Estimate scope before a standard/deep index: file count + rough tokens.

        Uses ``FilesystemScanner`` to count code files and a cheap bytes→tokens
        heuristic (~4 bytes/token) from each file's size — no file contents are
        read.
        """
        return await asyncio.to_thread(self._preview_sync)

    def _preview_sync(self) -> dict:
        from velune.repository.scanner import FilesystemScanner

        files = FilesystemScanner(self.root_path).scan_code_files()
        total_bytes = 0
        for f in files:
            try:
                total_bytes += f.stat().st_size
            except OSError:
                continue
        est_tokens = total_bytes // 4
        return {
            "file_count": len(files),
            "total_bytes": total_bytes,
            "est_tokens": est_tokens,
        }

    async def run_incremental(self, progress_callback=None) -> object:
        """Compute and apply a file-level symbol index on demand (``standard`` mode).

        Returns the applied :class:`IndexDelta`. Runs synchronously to
        completion (the caller decides whether to wrap it in a background job).
        """
        from velune.repository.incremental_indexer import IncrementalIndexer

        inc = IncrementalIndexer(self.workspace_root, self._state_path)
        delta = await inc.compute_delta()
        if delta.is_empty:
            self._last_delta = delta
            return delta
        if progress_callback is not None:
            inc.progress_callback = progress_callback
        await inc.apply_delta(delta)
        self._last_delta = delta
        return delta

    async def run_deep(self) -> RepositorySnapshot:
        """Full repository cognition (``deep`` mode): symbols + graph + architecture.

        Wraps the synchronous :meth:`index` pipeline in a worker thread so the
        REPL event loop stays responsive.
        """
        return await asyncio.to_thread(self.index, True)

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

            # Write a minimal retrieval index so BM25 is non-empty even on cold start.
            # The full symbol-enriched index is written when index() runs synchronously;
            # this lightweight version ensures file-path + language tokens are available
            # immediately after the background pass finishes.
            retrieval_path = self.root_path / ".velune" / "retrieval_index.json"
            if not retrieval_path.exists():
                try:
                    from velune.repository.index_state import IndexState

                    saved_state = IndexState.load(self._state_path)
                    if saved_state and saved_state.file_index:
                        docs = []
                        for rel_path, entry in saved_state.file_index.items():
                            filename = rel_path.rsplit("/", 1)[-1]
                            docs.append(
                                {
                                    "id": rel_path,
                                    "content": f"{filename} {entry.language} {rel_path}",
                                    "metadata": {
                                        "path": rel_path,
                                        "language": entry.language,
                                        "symbols": [],
                                        "size_bytes": 0,
                                    },
                                }
                            )
                        retrieval_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(retrieval_path, "w", encoding="utf-8") as fh:
                            json.dump(docs, fh)
                        logger.info("Cold-start retrieval index written: %d documents", len(docs))
                except Exception as exc:
                    logger.debug("Could not write cold-start retrieval index: %s", exc)

            # Keep last_delta so the next orchestrator run can surface it in context,
            # then clear it so subsequent runs don't re-announce the same changes.
            # (The orchestrator reads last_delta before calling index(), so the order is safe.)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Background incremental indexing failed: %s", exc)
