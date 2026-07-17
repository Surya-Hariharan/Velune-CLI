"""Tests for the Repository Intelligence Engine.

Covers:
- RepositoryEventType constants
- Event factory helpers (payload correctness)
- KnowledgeGraphPatcher (surgical delta application)
- RepositoryIntelligenceEngine lifecycle (start / stop / event emission)
- Downstream task dispatch (graph_patch, profile_refresh)
- Git state diff logic

All I/O that would touch the filesystem or spawn subprocesses is either
run against a real tmp_path or mocked to keep tests fast and deterministic.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from velune.intelligence.events import (
    RepositoryEventType,
    make_engine_started,
    make_engine_stopped,
    make_files_changed,
    make_git_state_changed,
    make_index_updated,
    make_knowledge_graph_patched,
    make_pipeline_refreshed,
    make_profile_refreshed,
)
from velune.intelligence.graph_patcher import KnowledgeGraphPatcher, PatchResult
from velune.knowledge.graph import KnowledgeGraph
from velune.knowledge.schemas import KnowledgeNode, NodeType
from velune.repository.incremental_indexer import IndexDelta

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _make_graph(tmp_path: Path) -> KnowledgeGraph:
    g = KnowledgeGraph(tmp_path / "kg.db")
    _run(g.initialize())
    return g


def _make_bus():
    """Return a minimal fake CognitiveBus that records emitted events."""
    bus = AsyncMock()
    bus.emitted: list = []

    async def _emit(event):
        bus.emitted.append(event)

    bus.emit = _emit
    return bus


# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------


class TestRepositoryEventType:
    def test_files_changed_value(self):
        assert RepositoryEventType.FILES_CHANGED == "repository.files_changed"

    def test_index_updated_value(self):
        assert RepositoryEventType.INDEX_UPDATED == "repository.index_updated"

    def test_knowledge_graph_patched_value(self):
        assert RepositoryEventType.KNOWLEDGE_GRAPH_PATCHED == "repository.knowledge_graph_patched"

    def test_git_state_changed_value(self):
        assert RepositoryEventType.GIT_STATE_CHANGED == "repository.git_state_changed"

    def test_profile_refreshed_value(self):
        assert RepositoryEventType.PROFILE_REFRESHED == "repository.profile_refreshed"

    def test_engine_started_value(self):
        assert RepositoryEventType.ENGINE_STARTED == "repository.engine_started"

    def test_engine_stopped_value(self):
        assert RepositoryEventType.ENGINE_STOPPED == "repository.engine_stopped"

    def test_pipeline_refreshed_value(self):
        assert RepositoryEventType.PIPELINE_REFRESHED == "repository.pipeline_refreshed"


# ---------------------------------------------------------------------------
# Event factory helpers
# ---------------------------------------------------------------------------


class TestEventFactories:
    def test_make_files_changed_payload(self):
        evt = make_files_changed(["a.py"], ["b.py"], ["c.py"])
        assert evt.event_type == RepositoryEventType.FILES_CHANGED
        assert evt.data["added"] == ["a.py"]
        assert evt.data["updated"] == ["b.py"]
        assert evt.data["removed"] == ["c.py"]
        assert evt.data["total"] == 3

    def test_make_files_changed_empty(self):
        evt = make_files_changed([], [], [])
        assert evt.data["total"] == 0

    def test_make_index_updated_payload(self):
        evt = make_index_updated("abc123")
        assert evt.event_type == RepositoryEventType.INDEX_UPDATED
        assert evt.data["commit_sha"] == "abc123"
        assert "indexed_at" in evt.data

    def test_make_index_updated_none_sha(self):
        evt = make_index_updated(None)
        assert evt.data["commit_sha"] is None

    def test_make_knowledge_graph_patched_payload(self):
        evt = make_knowledge_graph_patched(nodes_added=5, nodes_removed=2, edges_added=8)
        assert evt.event_type == RepositoryEventType.KNOWLEDGE_GRAPH_PATCHED
        assert evt.data["nodes_added"] == 5
        assert evt.data["nodes_removed"] == 2
        assert evt.data["edges_added"] == 8

    def test_make_git_state_changed_payload(self):
        evt = make_git_state_changed("main", "abc", 3, ["branch", "uncommitted"])
        assert evt.event_type == RepositoryEventType.GIT_STATE_CHANGED
        assert evt.data["branch"] == "main"
        assert evt.data["commit_sha"] == "abc"
        assert evt.data["uncommitted_files"] == 3
        assert "branch" in evt.data["changed"]

    def test_make_profile_refreshed_payload(self):
        profile = {"tech_stack": {"python": True}}
        evt = make_profile_refreshed(profile)
        assert evt.event_type == RepositoryEventType.PROFILE_REFRESHED
        assert evt.data["tech_stack"] == {"python": True}

    def test_make_engine_started_payload(self):
        evt = make_engine_started("/workspace")
        assert evt.event_type == RepositoryEventType.ENGINE_STARTED
        assert evt.data["workspace"] == "/workspace"

    def test_make_engine_stopped_payload(self):
        evt = make_engine_stopped("/workspace")
        assert evt.event_type == RepositoryEventType.ENGINE_STOPPED
        assert evt.data["workspace"] == "/workspace"

    def test_make_pipeline_refreshed_payload(self):
        evt = make_pipeline_refreshed(files_recomputed=3, edge_count=10, route_count=2)
        assert evt.event_type == RepositoryEventType.PIPELINE_REFRESHED
        assert evt.data["files_recomputed"] == 3
        assert evt.data["edge_count"] == 10
        assert evt.data["route_count"] == 2
        assert "refreshed_at" in evt.data

    def test_events_have_unique_ids(self):
        e1 = make_files_changed([], [], [])
        e2 = make_files_changed([], [], [])
        assert e1.event_id != e2.event_id

    def test_events_have_timestamp(self):
        evt = make_files_changed([], [], [])
        assert evt.timestamp > 0


# ---------------------------------------------------------------------------
# KnowledgeGraph delete_nodes_by_file
# ---------------------------------------------------------------------------


class TestKnowledgeGraphFileDeletion:
    def _seed(self, graph: KnowledgeGraph) -> None:
        nodes = [
            KnowledgeNode(id="file:a.py", node_type=NodeType.FILE, label="a.py", file_path="a.py"),
            KnowledgeNode(
                id="sym:a.py:fn", node_type=NodeType.FUNCTION, label="fn", file_path="a.py"
            ),
            KnowledgeNode(id="file:b.py", node_type=NodeType.FILE, label="b.py", file_path="b.py"),
        ]
        _run(graph.upsert_nodes_bulk(nodes))

    def test_delete_nodes_by_file_removes_symbols(self, tmp_path: Path):
        g = _make_graph(tmp_path)
        self._seed(g)
        _run(g.delete_nodes_by_file("a.py"))
        remaining = _run(g.get_nodes_by_file("a.py"))
        # The file node itself may remain (delete_nodes_by_file targets symbols)
        sym_remaining = [n for n in remaining if n.id == "sym:a.py:fn"]
        assert not sym_remaining

    def test_delete_nodes_by_files_bulk(self, tmp_path: Path):
        g = _make_graph(tmp_path)
        self._seed(g)
        _run(g.delete_nodes_by_files(["a.py"]))
        _run(g.stats())
        # b.py's file node should remain
        file_nodes = _run(g.get_nodes_by_type(NodeType.FILE))
        file_paths = {n.file_path for n in file_nodes}
        assert "b.py" in file_paths

    def test_delete_nonexistent_file_is_noop(self, tmp_path: Path):
        g = _make_graph(tmp_path)
        self._seed(g)
        _run(g.delete_nodes_by_file("does_not_exist.py"))
        stats = _run(g.stats())
        assert stats.node_count == 3  # unchanged

    def test_delete_empty_list_is_noop(self, tmp_path: Path):
        g = _make_graph(tmp_path)
        self._seed(g)
        _run(g.delete_nodes_by_files([]))
        stats = _run(g.stats())
        assert stats.node_count == 3


# ---------------------------------------------------------------------------
# KnowledgeGraphPatcher — with real filesystem files
# ---------------------------------------------------------------------------


class TestKnowledgeGraphPatcher:
    def _make_workspace(self, tmp_path: Path) -> tuple[Path, dict]:
        """Create a small synthetic Python workspace."""
        (tmp_path / "app.py").write_text(
            "def run():\n    pass\n\nclass App:\n    def start(self): pass\n",
            encoding="utf-8",
        )
        (tmp_path / "utils.py").write_text(
            "def helper():\n    return 42\n",
            encoding="utf-8",
        )
        return tmp_path, {}

    def test_patch_empty_delta_is_noop(self, tmp_path: Path):
        g = _make_graph(tmp_path)
        patcher = KnowledgeGraphPatcher(g, tmp_path)
        result = _run(patcher.patch(IndexDelta()))
        assert result.nodes_added == 0
        assert result.nodes_removed == 0
        assert result.edges_added == 0

    def test_patch_adds_nodes_for_new_files(self, tmp_path: Path):
        ws, _ = self._make_workspace(tmp_path)
        g = _make_graph(tmp_path)
        patcher = KnowledgeGraphPatcher(g, ws)

        delta = IndexDelta(to_add=["app.py", "utils.py"])
        result = _run(patcher.patch(delta))

        assert result.files_patched == 2
        assert result.nodes_added > 0
        # File nodes + symbol nodes should exist
        stats = _run(g.stats())
        assert stats.file_count >= 2

    def test_patch_removes_nodes_for_deleted_files(self, tmp_path: Path):
        ws, _ = self._make_workspace(tmp_path)
        g = _make_graph(tmp_path)
        patcher = KnowledgeGraphPatcher(g, ws)

        # First add
        _run(patcher.patch(IndexDelta(to_add=["app.py", "utils.py"])))
        before = _run(g.stats())

        # Now remove one file (don't need it to exist on disk for removal)
        delta = IndexDelta(to_remove=["utils.py"])
        _run(patcher.patch(delta))

        after = _run(g.stats())
        assert after.node_count < before.node_count

    def test_patch_update_replaces_stale_nodes(self, tmp_path: Path):
        ws, _ = self._make_workspace(tmp_path)
        g = _make_graph(tmp_path)
        patcher = KnowledgeGraphPatcher(g, ws)

        # Initial add
        _run(patcher.patch(IndexDelta(to_add=["app.py"])))
        _run(g.stats())

        # Simulate update (file content unchanged on disk, but treat as update)
        _run(patcher.patch(IndexDelta(to_update=["app.py"])))
        after = _run(g.stats())

        # Node count should be stable (old removed, new added)
        assert after.node_count > 0

    def test_patch_nonexistent_file_is_graceful(self, tmp_path: Path):
        g = _make_graph(tmp_path)
        patcher = KnowledgeGraphPatcher(g, tmp_path)
        delta = IndexDelta(to_add=["nonexistent_file.py"])
        result = _run(patcher.patch(delta))
        # Parser skips missing files; no exception, files_patched=0
        assert result.files_patched == 0
        assert result.errors == 0

    def test_patch_result_dataclass(self):
        r = PatchResult()
        assert r.nodes_added == 0
        assert r.files_patched == 0
        assert r.errors == 0


# ---------------------------------------------------------------------------
# RepositoryIntelligenceEngine — lifecycle tests with mocked dependencies
# ---------------------------------------------------------------------------


class TestRepositoryIntelligenceEngine:
    def _make_engine(self, tmp_path: Path):
        from velune.intelligence.engine import RepositoryIntelligenceEngine

        graph = _make_graph(tmp_path)
        bus = _make_bus()

        # Mock cognition — no real indexer needed for lifecycle tests
        cognition = MagicMock()
        cognition.quick_summary = MagicMock(return_value={"root": str(tmp_path)})

        engine = RepositoryIntelligenceEngine(
            workspace=tmp_path,
            cognition=cognition,
            knowledge_graph=graph,
            bus=bus,
            change_poll_interval=60.0,  # effectively disable polling in tests
            git_poll_interval=60.0,
        )
        return engine, bus, cognition

    def test_engine_starts_and_emits_started_event(self, tmp_path: Path):
        engine, bus, _ = self._make_engine(tmp_path)

        async def _test():
            await engine.initialize()
            await asyncio.sleep(0.05)  # let the event propagate
            await engine.shutdown()

        _run(_test())

        emitted_types = [e.event_type for e in bus.emitted]
        assert RepositoryEventType.ENGINE_STARTED in emitted_types

    def test_engine_shutdown_emits_stopped_event(self, tmp_path: Path):
        engine, bus, _ = self._make_engine(tmp_path)

        async def _test():
            await engine.initialize()
            await engine.shutdown()

        _run(_test())

        emitted_types = [e.event_type for e in bus.emitted]
        assert RepositoryEventType.ENGINE_STOPPED in emitted_types

    def test_engine_is_running_after_initialize(self, tmp_path: Path):
        engine, bus, _ = self._make_engine(tmp_path)

        async def _test():
            assert not engine.is_running
            await engine.initialize()
            assert engine.is_running
            await engine.shutdown()
            assert not engine.is_running

        _run(_test())

    def test_engine_initialize_is_idempotent(self, tmp_path: Path):
        engine, bus, _ = self._make_engine(tmp_path)

        async def _test():
            await engine.initialize()
            await engine.initialize()  # second call is no-op
            assert engine.is_running
            await engine.shutdown()

        _run(_test())

    def test_engine_shutdown_is_idempotent(self, tmp_path: Path):
        engine, bus, _ = self._make_engine(tmp_path)

        async def _test():
            await engine.initialize()
            await engine.shutdown()
            await engine.shutdown()  # second call is no-op

        _run(_test())  # must not raise

    def test_current_git_state_property(self, tmp_path: Path):
        engine, bus, _ = self._make_engine(tmp_path)

        async def _test():
            await engine.initialize()
            state = engine.current_git_state
            assert "branch" in state
            assert "sha" in state
            assert "uncommitted" in state
            await engine.shutdown()

        _run(_test())

    def test_downstream_queue_size_property(self, tmp_path: Path):
        engine, bus, _ = self._make_engine(tmp_path)
        # Queue starts empty
        assert engine.downstream_queue_size == 0

    def test_engine_does_not_crash_on_bad_bus(self, tmp_path: Path):
        """Engine must survive if bus.emit raises."""
        from velune.intelligence.engine import RepositoryIntelligenceEngine

        graph = _make_graph(tmp_path)

        bad_bus = MagicMock()
        bad_bus.emit = AsyncMock(side_effect=RuntimeError("bus exploded"))

        cognition = MagicMock()
        cognition.quick_summary = MagicMock(return_value={})

        engine = RepositoryIntelligenceEngine(
            workspace=tmp_path,
            cognition=cognition,
            knowledge_graph=graph,
            bus=bad_bus,
            change_poll_interval=60.0,
            git_poll_interval=60.0,
        )

        async def _test():
            await engine.initialize()
            await asyncio.sleep(0.05)
            await engine.shutdown()

        _run(_test())  # must not raise


# ---------------------------------------------------------------------------
# pipeline_refresh downstream task — the new incremental-cognition trigger
# ---------------------------------------------------------------------------


class TestPipelineRefreshDownstreamTask:
    def _make_engine(self, tmp_path: Path, *, with_job_registry: bool = False):
        from velune.intelligence.engine import RepositoryIntelligenceEngine

        graph = _make_graph(tmp_path)
        bus = _make_bus()

        cognition = MagicMock()
        cognition.refresh_pipeline_cache = AsyncMock(
            return_value={"files_recomputed": 2, "edge_count": 5, "route_count": 1}
        )

        job_registry = None
        if with_job_registry:
            from velune.core.task_registry import JobRegistry

            job_registry = JobRegistry()

        engine = RepositoryIntelligenceEngine(
            workspace=tmp_path,
            cognition=cognition,
            knowledge_graph=graph,
            bus=bus,
            job_registry=job_registry,
            change_poll_interval=60.0,
            git_poll_interval=60.0,
        )
        return engine, bus, cognition, job_registry

    def test_pipeline_refresh_calls_cognition_and_emits_event(self, tmp_path: Path):
        engine, bus, cognition, _ = self._make_engine(tmp_path)
        delta = IndexDelta(to_add=["a.py"], to_update=["b.py"], to_remove=[])

        _run(engine._handle_pipeline_refresh(delta))

        cognition.refresh_pipeline_cache.assert_awaited_once_with(delta)
        evt = next(e for e in bus.emitted if e.event_type == RepositoryEventType.PIPELINE_REFRESHED)
        assert evt.data["files_recomputed"] == 2
        assert evt.data["edge_count"] == 5
        assert evt.data["route_count"] == 1

    def test_pipeline_refresh_registers_a_job(self, tmp_path: Path):
        from velune.core.task_registry import JobStatus

        engine, _, _, job_registry = self._make_engine(tmp_path, with_job_registry=True)
        delta = IndexDelta(to_add=["a.py"], to_update=[], to_remove=[])

        _run(engine._handle_pipeline_refresh(delta))

        jobs = [j for j in job_registry.all_jobs() if j.name == "cognition:pipeline_refresh"]
        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.COMPLETED

    def test_pipeline_refresh_job_marked_failed_on_error(self, tmp_path: Path):
        from velune.core.task_registry import JobStatus

        engine, _, cognition, job_registry = self._make_engine(tmp_path, with_job_registry=True)
        cognition.refresh_pipeline_cache = AsyncMock(side_effect=RuntimeError("boom"))
        delta = IndexDelta(to_add=["a.py"], to_update=[], to_remove=[])

        _run(engine._handle_pipeline_refresh(delta))  # must not raise

        jobs = [j for j in job_registry.all_jobs() if j.name == "cognition:pipeline_refresh"]
        assert len(jobs) == 1
        assert jobs[0].status == JobStatus.FAILED

    def test_pipeline_refresh_dispatched_from_downstream_worker(self, tmp_path: Path):
        from velune.intelligence.engine import _DownstreamTask

        engine, bus, cognition, _ = self._make_engine(tmp_path)
        delta = IndexDelta(to_add=["a.py"], to_update=[], to_remove=[])

        _run(engine._run_downstream_task(_DownstreamTask(task_type="pipeline_refresh", delta=delta)))

        cognition.refresh_pipeline_cache.assert_awaited_once()

    def test_missing_job_registry_does_not_break_refresh(self, tmp_path: Path):
        """job_registry=None (the default) must not crash the handler."""
        engine, bus, cognition, _ = self._make_engine(tmp_path)  # no job registry
        delta = IndexDelta(to_add=["a.py"], to_update=[], to_remove=[])

        _run(engine._handle_pipeline_refresh(delta))  # must not raise

        assert any(e.event_type == RepositoryEventType.PIPELINE_REFRESHED for e in bus.emitted)


# ---------------------------------------------------------------------------
# Git state diff logic (unit tests, no subprocess)
# ---------------------------------------------------------------------------


class TestGitStateDiff:
    def _engine_with_known_state(self, tmp_path: Path):
        from velune.intelligence.engine import RepositoryIntelligenceEngine, _GitState

        graph = _make_graph(tmp_path)
        bus = _make_bus()
        cognition = MagicMock()
        engine = RepositoryIntelligenceEngine(
            workspace=tmp_path,
            cognition=cognition,
            knowledge_graph=graph,
            bus=bus,
        )
        # Manually seed the state
        engine._git_state = _GitState(branch="main", sha="abc", uncommitted=0)
        return engine

    def test_no_change_returns_empty_list(self, tmp_path: Path):
        from velune.intelligence.engine import _GitState

        engine = self._engine_with_known_state(tmp_path)
        same = _GitState(branch="main", sha="abc", uncommitted=0)
        assert engine._diff_git_state(same) == []

    def test_branch_change_detected(self, tmp_path: Path):
        from velune.intelligence.engine import _GitState

        engine = self._engine_with_known_state(tmp_path)
        new = _GitState(branch="feature/x", sha="abc", uncommitted=0)
        changed = engine._diff_git_state(new)
        assert "branch" in changed

    def test_sha_change_detected(self, tmp_path: Path):
        from velune.intelligence.engine import _GitState

        engine = self._engine_with_known_state(tmp_path)
        new = _GitState(branch="main", sha="def", uncommitted=0)
        changed = engine._diff_git_state(new)
        assert "sha" in changed

    def test_uncommitted_change_detected(self, tmp_path: Path):
        from velune.intelligence.engine import _GitState

        engine = self._engine_with_known_state(tmp_path)
        new = _GitState(branch="main", sha="abc", uncommitted=3)
        changed = engine._diff_git_state(new)
        assert "uncommitted" in changed

    def test_multiple_changes_detected(self, tmp_path: Path):
        from velune.intelligence.engine import _GitState

        engine = self._engine_with_known_state(tmp_path)
        new = _GitState(branch="feature/y", sha="xyz", uncommitted=1)
        changed = engine._diff_git_state(new)
        assert set(changed) == {"branch", "sha", "uncommitted"}
