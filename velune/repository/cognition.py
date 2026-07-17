"""Repository cognition pipeline merging AST indices, Git history, and dependency graphs."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from velune.repository.incremental_indexer import IncrementalIndexer, IndexDelta

logger = logging.getLogger("velune.repository.cognition")

_STATE_FILENAME = "index_state.json"
_PIPELINE_CACHE_FILENAME = "pipeline_cache.json"
# Volatility reflects committed history, not local edits — recomputing it on
# every incremental refresh is pure waste. Cache it for a few minutes.
_VOLATILITY_TTL_SECONDS = 600.0
# Above this age, get_snapshot_fresh() still returns the cache (never blocks
# the turn on recompute) but flags it stale in cognition_freshness so /index
# status and the context builder can surface it.
_FRESHNESS_STALE_AFTER_SECONDS = 30.0


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

        # Pipeline cache: the persisted, delta-aware output of the grapher /
        # API mapper / architecture+tech detectors / git metrics (everything
        # _run_pipeline computes). Populated on cold start and kept current by
        # RepositoryIntelligenceEngine's background downstream worker calling
        # refresh_pipeline_cache() — see get_snapshot_fresh().
        self._pipeline_cache_path = self.root_path / ".velune" / _PIPELINE_CACHE_FILENAME
        self._volatility_cache: tuple[float, dict[str, int]] | None = None  # (cached_at, data)
        self.pipeline_cache_hits = 0
        self.pipeline_cache_misses = 0
        self.files_recomputed_last_run = 0

    @property
    def last_delta(self) -> object | None:
        """The IndexDelta from the most recent incremental index, or None."""
        return self._last_delta

    def consume_delta(self) -> object | None:
        """Return the pending delta and clear it, so it is announced only once.

        Exists because callers genuinely need this and the property had no
        setter — which is why ``cognition/orchestrator.py`` used to reach across
        packages and poke ``_last_delta`` directly.
        """
        delta, self._last_delta = self._last_delta, None
        return delta

    # ------------------------------------------------------------------
    # Lifecycle protocol (called by LifecycleCoordinator)
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Lifecycle hook — intentionally inert.

        Indexing is not kicked off from here, so that constructing the service
        (which happens on every launch, including outside a project) costs
        nothing. The *triggers* live elsewhere and are real: the REPL submits a
        first index on entry via ``auto_detect_on_entry``, and
        ``RepositoryIntelligenceEngine`` polls for changes thereafter. The
        methods below are the on-demand entry points behind ``/index``.
        """
        logger.debug("RepositoryCognitionService.initialize: no-op (indexing starts on demand).")
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

        # An index already running is *progress*, not an obstacle. This used to
        # cancel the in-flight task and start over — and since apply_delta only
        # persists at the very end, and the orchestrator probes on every prompt,
        # a repo that took longer to index than the user's typing interval would
        # be cancelled and restarted forever, never finishing. Let it run.
        if self._bg_index_task and not self._bg_index_task.done():
            logger.debug("probe_for_changes: an index is already running — leaving it alone.")
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
            except asyncio.CancelledError:
                # Expected — we are the one who cancelled it. Kept separate from
                # the handler below: catching (CancelledError, Exception) as one
                # bucket also swallowed a cancellation aimed at *shutdown itself*,
                # which silently broke cooperative shutdown of the caller.
                pass
            except Exception as exc:
                logger.debug("Background index task failed during shutdown: %s", exc)

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

        **This is synchronous and slow** — a full tree walk, a SHA-256 of every
        file, tree-sitter parsing, and several git subprocesses. Async callers
        must go through :meth:`run_deep` / :meth:`run_incremental`, or wrap it in
        ``asyncio.to_thread``; calling it directly from a coroutine freezes the
        REPL for as long as the walk takes.
        """
        from velune.repository.incremental_indexer import IncrementalIndexer

        # Refuse to walk an unbounded tree. probe_for_changes() and the CLI
        # handler both check this, but index() is reachable directly (the
        # orchestrator and the MCP server call it), and in $HOME or C:\ that
        # means recursively hashing the entire drive.
        reason = self.unsafe_reason()
        if reason:
            logger.warning("Refusing to index — workspace is %s.", reason)
            return RepositorySnapshot(root_path=str(self.root_path))

        inc = IncrementalIndexer(self.root_path, self._state_path)

        if not force and inc.git_sha() == self._stored_commit_sha():
            clean = inc.working_tree_is_clean()
            if clean:
                cached = self.get_snapshot()
                if cached:
                    logger.debug("Fast path: reusing cached snapshot (git SHA matches).")
                    return self._run_pipeline(cached)

        # Slow path: file-level incremental index
        snapshot = self.indexer.index(force=force)

        # Persist updated IndexState so the next session benefits from the fast path
        self._persist_index_state(inc, snapshot)

        return self._run_pipeline(snapshot)

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
    # Fast, cache-first snapshot for the interactive turn path
    # ------------------------------------------------------------------

    async def get_snapshot_fresh(self) -> RepositorySnapshot | None:
        """Return a snapshot without rebuilding the pipeline, unless this is cold start.

        This is what the per-turn orchestrator call should use instead of
        :meth:`index`. It reads the persisted pipeline cache (grapher edges,
        API map, architecture/tech summary, git metrics) — maintained
        incrementally by ``RepositoryIntelligenceEngine``'s background worker
        via :meth:`refresh_pipeline_cache` — and merges it onto the file/symbol
        snapshot. No grapher/API-mapper/architecture/tech work runs here.

        Falls back to the synchronous full :meth:`index` pipeline only when no
        pipeline cache exists yet (true cold start, e.g. first prompt in a repo
        that has never been indexed) — a one-time cost, seeded into the cache
        so every subsequent call hits the fast path.

        The returned snapshot may be a few seconds stale relative to disk (the
        background engine converges on its own poll cadence) — bounded
        staleness is the intended trade for never blocking a prompt on a full
        rescan. Staleness is surfaced via ``summary["cognition_freshness"]``.
        """
        file_snapshot = self.get_snapshot()
        cache = await asyncio.to_thread(self._load_pipeline_cache)

        if file_snapshot is None or cache is None:
            self.pipeline_cache_misses += 1
            snapshot = await asyncio.to_thread(self.index, False)
            if snapshot is not None:
                await asyncio.to_thread(self._seed_pipeline_cache_from_snapshot, snapshot)
            return snapshot

        self.pipeline_cache_hits += 1
        self._merge_cache_onto_snapshot(file_snapshot, cache)
        return file_snapshot

    def _merge_cache_onto_snapshot(self, snapshot: RepositorySnapshot, cache: dict) -> None:
        """Apply a persisted pipeline cache's derived fields onto *snapshot* in place."""
        from velune.repository.api_mapper import APIConnectionMap

        try:
            snapshot.edges = [RepositoryEdge(**e) for e in cache.get("edges", [])]
        except Exception as exc:
            logger.debug("Pipeline cache had malformed edges (ignored): %s", exc)

        summary = cache.get("summary")
        if isinstance(summary, dict):
            snapshot.summary.update(summary)

        api_map_dict = cache.get("api_map")
        if api_map_dict:
            try:
                snapshot.api_map = APIConnectionMap.from_dict(api_map_dict)
            except Exception as exc:
                logger.debug("Pipeline cache had malformed api_map (ignored): %s", exc)

        computed_at = cache.get("computed_at", 0.0)
        age = max(0.0, time.time() - computed_at)
        snapshot.summary["cognition_freshness"] = {
            "computed_at": computed_at,
            "age_seconds": round(age, 1),
            "source_commit_sha": cache.get("source_commit_sha"),
            "stale": age > _FRESHNESS_STALE_AFTER_SECONDS,
            "cache_hit": True,
        }

    def _load_pipeline_cache(self) -> dict | None:
        try:
            if not self._pipeline_cache_path.exists():
                return None
            with open(self._pipeline_cache_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.debug("Could not load pipeline cache: %s", exc)
            return None

    def _save_pipeline_cache(self, cache: dict) -> None:
        try:
            self._pipeline_cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._pipeline_cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        except Exception as exc:
            logger.debug("Could not persist pipeline cache: %s", exc)

    def _seed_pipeline_cache_from_snapshot(self, snapshot: RepositorySnapshot) -> None:
        """Seed the pipeline cache from a snapshot the full :meth:`index` pipeline just built.

        Called once, on cold start, so every call after the first hits
        :meth:`get_snapshot_fresh`'s fast path instead of re-running :meth:`index`
        forever.
        """
        from velune.repository.incremental_indexer import IncrementalIndexer

        try:
            sha = IncrementalIndexer(self.root_path, self._state_path).git_sha()
        except Exception:
            sha = None

        cache = {
            "computed_at": time.time(),
            "source_commit_sha": sha,
            "edges": [e.model_dump() for e in snapshot.edges],
            "api_map": snapshot.api_map.to_dict()
            if snapshot.api_map is not None and hasattr(snapshot.api_map, "to_dict")
            else None,
            "summary": {
                k: v
                for k, v in snapshot.summary.items()
                if k in ("git", "architecture", "metrics", "api_map")
            },
        }
        self._save_pipeline_cache(cache)

    # ------------------------------------------------------------------
    # Incremental pipeline refresh — runs off the interactive path, driven by
    # RepositoryIntelligenceEngine's background downstream worker.
    # ------------------------------------------------------------------

    async def refresh_pipeline_cache(self, delta: IndexDelta) -> dict:
        """Incrementally update the persisted pipeline cache for *delta*.

        Recomputes only the analyzers whose cost actually scales with the
        files in *delta* (grapher edges, API map); everything else is either
        cheap regardless of repo size (architecture/tech detection — audited,
        no full-file-content I/O) or TTL-cached (git volatility). Returns a
        small stats dict for the caller to log/emit as an event.
        """
        return await asyncio.to_thread(self._refresh_pipeline_cache_sync, delta)

    def _refresh_pipeline_cache_sync(self, delta: IndexDelta) -> dict:
        from velune.repository.api_mapper import APIConnectionMap, APIMapper, render_api_map
        from velune.repository.incremental_indexer import IncrementalIndexer

        snapshot = self.get_snapshot()
        if snapshot is None:
            return {"files_recomputed": 0, "edge_count": 0, "route_count": 0}

        prev_cache = self._load_pipeline_cache()
        if prev_cache is None:
            # No baseline yet — this background refresh got here before any
            # cold-start full index ran, so self.grapher is still empty.
            # Patching it now would cache a graph containing only the delta's
            # files. Do the one-time full build instead (same work a cold-start
            # index() call would do); every refresh after this one can patch.
            snapshot = self._run_pipeline(snapshot)
            self._seed_pipeline_cache_from_snapshot(snapshot)
            return {
                "files_recomputed": len(snapshot.files),
                "edge_count": len(snapshot.edges),
                "route_count": len(snapshot.api_map.routes) if snapshot.api_map else 0,
            }

        changed = list(dict.fromkeys(list(delta.to_add) + list(delta.to_update)))
        removed = list(delta.to_remove)
        self.files_recomputed_last_run = len(changed) + len(removed)

        file_by_path = {f.path: f for f in snapshot.files}
        all_paths = [f.path for f in snapshot.files]

        # --- Grapher: patch in place instead of rebuilding from scratch ---
        for path in removed + changed:
            self.grapher.remove_file(path)
        for path in changed:
            f = file_by_path.get(path)
            if f is None:
                continue
            self.grapher.add_file(f.path, f.language.value, f.size_bytes)
            for sym in f.symbols:
                self.grapher.add_symbol(sym)
        self.grapher.resolve_import_dependencies(
            all_paths, snapshot.symbols, source_scope=set(changed)
        )

        edges = [
            RepositoryEdge(
                source=src,
                target=tgt,
                edge_type=data.get("edge_type", "depends"),
                weight=data.get("weight", 1.0),
            )
            for src, tgt, _key, data in self.grapher.graph.edges(keys=True, data=True)
        ]

        # --- API map: incremental rescan, scoped to changed/removed files ---
        prev_api_map = None
        if prev_cache.get("api_map"):
            try:
                prev_api_map = APIConnectionMap.from_dict(prev_cache["api_map"])
            except Exception as exc:
                logger.debug("Could not reuse previous api_map (full rescan): %s", exc)
        api_mapper = APIMapper(self.root_path)
        api_map = api_mapper.build_map_incremental(prev_api_map, changed, removed)
        api_map_data = {
            "route_count": len(api_map.routes),
            "frontend_call_count": len(api_map.frontend_calls),
            "db_query_count": len(api_map.db_queries),
            "connection_count": len(api_map.connections),
            "api_map_text": render_api_map(api_map, max_tokens=1500),
        }

        # --- Framework footprints: union, content read scoped to changed files ---
        code_files: dict[str, str] = {}
        for path in changed:
            f = file_by_path.get(path)
            if f is not None and f.size_bytes < 100_000:
                try:
                    code_files[path] = (self.root_path / path).read_text(
                        encoding="utf-8", errors="ignore"
                    )
                except Exception:
                    pass
        new_frameworks = (
            set(self.analyzer.detect_framework_footprint(code_files)) if code_files else set()
        )
        prev_frameworks = set(
            prev_cache.get("summary", {}).get("architecture", {}).get("frameworks_detected", [])
        )
        frameworks = sorted(prev_frameworks | new_frameworks)

        # --- Cheap regardless of repo size — always re-run in full ---
        layers = self.analyzer.classify_architecture_layers(all_paths)
        analyzer_edges = [(e.source, e.target) for e in edges]
        violations = self.analyzer.detect_dependency_violations(layers, analyzer_edges)
        tech_stack = TechnologyDetector(self.root_path).detect()
        arch_report = ArchitectureDetector(self.root_path, snapshot.files, tech_stack).detect()

        # --- Git metrics: volatility TTL-cached, rest stays live but cheap ---
        branch = self.tracker.get_active_branch()
        changes = self.tracker.get_uncommitted_changes()
        recent_commits = self.tracker.get_recent_commits(limit=5)
        git_sha = recent_commits[0]["hash"] if recent_commits else None
        if git_sha is None:
            try:
                git_sha = IncrementalIndexer(self.root_path, self._state_path).git_sha()
            except Exception:
                git_sha = None

        all_volatility = self._get_volatility_cached()
        file_volatility: dict[str, int] = {}
        for f in snapshot.files:
            file_volatility[f.path] = all_volatility.get(f.path, 0) or all_volatility.get(
                f.path.replace("/", "\\"), 0
            )

        summary = {
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

        cache = {
            "computed_at": time.time(),
            "source_commit_sha": git_sha,
            "edges": [e.model_dump() for e in edges],
            "api_map": api_map.to_dict(),
            "summary": summary,
        }
        self._save_pipeline_cache(cache)

        return {
            "files_recomputed": self.files_recomputed_last_run,
            "edge_count": len(edges),
            "route_count": len(api_map.routes),
        }

    def _get_volatility_cached(self) -> dict[str, int]:
        """TTL-cached ``GitTracker.get_all_file_volatility`` — it reflects commit
        history, not local edits, so recomputing it on every refresh is waste."""
        now = time.time()
        if self._volatility_cache is not None:
            cached_at, data = self._volatility_cache
            if now - cached_at < _VOLATILITY_TTL_SECONDS:
                return data
        data = self.tracker.get_all_file_volatility(days=90)
        self._volatility_cache = (now, data)
        return data

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_pipeline(self, snapshot: RepositorySnapshot) -> RepositorySnapshot:
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
        all_volatility = self._get_volatility_cached()
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

        # API connection map — frontend ↔ backend ↔ database chain.
        #
        # `snapshot.api_map` must be a declared field on RepositorySnapshot; when
        # it wasn't, this assignment raised on every run and the broad handler
        # below reduced a dead feature to a debug line nobody read. The build is
        # still best-effort (a parse failure in one repo shouldn't fail the whole
        # index), but a programming error here — a missing field, a bad
        # signature — is not "non-fatal", so those propagate.
        api_map_data: dict = {}
        try:
            from velune.repository.api_mapper import APIMapper, render_api_map

            api_mapper = APIMapper(self.root_path)
            api_map = api_mapper.build_map(file_paths)
            snapshot.api_map = api_map
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
        except (OSError, UnicodeDecodeError, re.error) as exc:
            logger.warning("API mapping failed (non-fatal): %s", exc)

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

    def _persist_index_state(self, inc: IncrementalIndexer, snapshot: RepositorySnapshot) -> None:
        """Update IndexState on disk after a full index run."""
        import time

        from velune.repository.index_state import IndexedFile, IndexState

        try:
            git_sha = inc.git_sha()
            state = IndexState.load(self._state_path) or IndexState.empty(str(self.root_path))
            now = time.time()

            file_index: dict[str, IndexedFile] = {}
            for f in snapshot.files:
                # Record the mtime/size alongside the hash, so the next delta
                # computation can skip this file with a stat() instead of
                # re-reading and re-hashing it. Missing them here would leave
                # every fully-indexed file without a fast signal.
                try:
                    st = (self.root_path / f.path).stat()
                    mtime, size = st.st_mtime, st.st_size
                except OSError:
                    mtime, size = 0.0, 0

                file_index[f.path] = IndexedFile(
                    path=f.path,
                    content_hash=f.sha256,
                    language=f.language.value,
                    symbol_count=len(f.symbols),
                    indexed_at=now,
                    mtime=mtime,
                    size=size,
                )

            state.file_index = file_index
            state.touch(git_sha)
            state.workspace_root = str(self.root_path)
            state.save(self._state_path)
        except Exception as exc:
            logger.debug("Could not persist IndexState: %s", exc)

    async def _background_apply(
        self,
        inc: IncrementalIndexer,
        delta: IndexDelta,
        n: int,
    ) -> None:
        """Apply a delta in the background. Reports through logs and the job registry.

        This used to build its own ``Console(stderr=True)`` and drive a
        ``transient=True`` Rich progress bar on it. That console bypassed the
        REPL's ``_ConsoleSink`` — whose entire job is to strip non-SGR CSI
        sequences — and ``transient`` means Rich emits cursor-up and erase-line
        escapes directly into the terminal. Fired from ``auto_detect_on_entry``
        on the default REPL-entry path, it wrote those escapes straight over the
        fullscreen prompt_toolkit UI.

        A background task the user never asked for has no business drawing on the
        screen. Progress is available via ``/index status`` and ``/dashboard``,
        which read the job registry.
        """
        try:
            await inc.apply_delta(delta)
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
                                        "size_bytes": entry.size,
                                    },
                                }
                            )
                        retrieval_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(retrieval_path, "w", encoding="utf-8") as fh:
                            json.dump(docs, fh)
                        logger.info("Cold-start retrieval index written: %d documents", len(docs))
                except Exception as exc:
                    logger.debug("Could not write cold-start retrieval index: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Background incremental indexing failed: %s", exc)
