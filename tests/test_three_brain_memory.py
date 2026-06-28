"""Tests for the Three-Brain Memory Coordinator.

Covers:
- ThreeBrainResult construction and rendering
- ThreeBrainCoordinator query fan-out
- Bus subscription for file staleness tracking
- Graceful degradation when individual brains are None or raise
- Working/semantic/episodic brain query routing
- KG brain integration
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from velune.memory.three_brain import ThreeBrainCoordinator, ThreeBrainResult

# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_turn(role: str = "user", content: str = "hello") -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content)


def _make_retrieved(
    content: str = "memory content", attribution: str = "just now"
) -> SimpleNamespace:
    return SimpleNamespace(content=content, attribution=attribution, distance=0.1)


def _make_episodic_turn(role: str = "user", content: str = "old convo") -> SimpleNamespace:
    return SimpleNamespace(role=role, content=content, created_at=0.0)


def _make_bus() -> MagicMock:
    bus = MagicMock()
    bus.subscribe = AsyncMock()
    return bus


def _make_event(added: list[str], updated: list[str], removed: list[str]) -> SimpleNamespace:
    return SimpleNamespace(data={"added": added, "updated": updated, "removed": removed})


# ---------------------------------------------------------------------------
# ThreeBrainResult unit tests
# ---------------------------------------------------------------------------


class TestThreeBrainResult:
    def test_total_hits_empty(self):
        r = ThreeBrainResult()
        assert r.total_hits() == 0

    def test_total_hits_counts_across_all_brains(self):
        r = ThreeBrainResult(
            working_hits=[_make_turn()],
            semantic_hits=[_make_retrieved(), _make_retrieved()],
            episodic_hits=[_make_episodic_turn()],
        )
        assert r.total_hits() == 4

    def test_is_empty_true_when_no_hits_no_kg(self):
        assert ThreeBrainResult().is_empty()

    def test_is_empty_false_when_kg_context_present(self):
        r = ThreeBrainResult(kg_context="some code context")
        assert not r.is_empty()

    def test_is_empty_false_when_working_hits_present(self):
        r = ThreeBrainResult(working_hits=[_make_turn()])
        assert not r.is_empty()

    def test_as_context_block_empty_result(self):
        assert ThreeBrainResult().as_context_block() == ""

    def test_as_context_block_includes_working_section(self):
        r = ThreeBrainResult(working_hits=[_make_turn("user", "fix the bug")])
        block = r.as_context_block()
        assert "Working Memory" in block
        assert "fix the bug" in block

    def test_as_context_block_includes_semantic_section(self):
        r = ThreeBrainResult(semantic_hits=[_make_retrieved("vector result", "2 hours ago")])
        block = r.as_context_block()
        assert "Semantic Memory" in block
        assert "vector result" in block

    def test_as_context_block_includes_episodic_section(self):
        r = ThreeBrainResult(episodic_hits=[_make_episodic_turn("assistant", "I fixed it")])
        block = r.as_context_block()
        assert "Episodic Memory" in block
        assert "I fixed it" in block

    def test_as_context_block_includes_kg_context(self):
        r = ThreeBrainResult(kg_context="Graph: 42 files, 200 symbols")
        block = r.as_context_block()
        assert "Code Graph Context" in block
        assert "42 files" in block

    def test_as_context_block_staleness_annotation(self):
        r = ThreeBrainResult(
            working_hits=[_make_turn()],
            stale_file_count=3,
        )
        block = r.as_context_block()
        assert "3 file(s) changed" in block

    def test_as_context_block_no_staleness_when_zero(self):
        r = ThreeBrainResult(working_hits=[_make_turn()], stale_file_count=0)
        block = r.as_context_block()
        assert "changed" not in block

    def test_as_context_block_truncated_at_max_chars(self):
        # Each section header + 3 turns (capped at 120 chars each) = ~400+ chars per brain.
        # Three brains + KG context will comfortably exceed max_chars=300.
        many_turns = [_make_turn("user", "x" * 120) for _ in range(5)]
        semantic_hits = [_make_retrieved("y" * 120) for _ in range(5)]
        episodic_hits = [_make_episodic_turn("user", "z" * 100) for _ in range(5)]
        r = ThreeBrainResult(
            working_hits=many_turns,
            semantic_hits=semantic_hits,
            episodic_hits=episodic_hits,
            kg_context="w" * 600,
        )
        block = r.as_context_block(max_chars=300)
        assert len(block) <= 320
        assert "truncated" in block

    def test_as_context_block_shows_last_3_working_turns(self):
        turns = [_make_turn("user", f"turn {i}") for i in range(6)]
        r = ThreeBrainResult(working_hits=turns)
        block = r.as_context_block()
        # Only the last 3 should appear
        assert "turn 5" in block
        assert "turn 3" in block
        assert "turn 0" not in block

    def test_query_field_stored(self):
        r = ThreeBrainResult(query="how does auth work")
        assert r.query == "how does auth work"


# ---------------------------------------------------------------------------
# ThreeBrainCoordinator — construction and properties
# ---------------------------------------------------------------------------


class TestThreeBrainCoordinatorProperties:
    def test_stale_file_count_zero_initially(self):
        coord = ThreeBrainCoordinator(None, None, None)
        assert coord.stale_file_count == 0

    def test_stale_paths_empty_initially(self):
        coord = ThreeBrainCoordinator(None, None, None)
        assert coord.stale_paths == frozenset()

    def test_clear_stale_resets_count(self):
        coord = ThreeBrainCoordinator(None, None, None)
        coord._stale_paths = {"a.py", "b.py"}
        coord.clear_stale()
        assert coord.stale_file_count == 0

    def test_stale_paths_is_immutable_view(self):
        coord = ThreeBrainCoordinator(None, None, None)
        coord._stale_paths = {"x.py"}
        snap = coord.stale_paths
        assert isinstance(snap, frozenset)
        assert "x.py" in snap


# ---------------------------------------------------------------------------
# Bus subscription and staleness tracking
# ---------------------------------------------------------------------------


class TestBusSubscription:
    async def test_subscribe_calls_bus_subscribe(self):
        coord = ThreeBrainCoordinator(None, None, None)
        bus = _make_bus()
        await coord.subscribe_to_repository_events(bus)
        bus.subscribe.assert_called_once()
        topic, _ = bus.subscribe.call_args[0]
        assert topic == "repository.files_changed"

    async def test_files_changed_event_updates_stale_paths(self):
        coord = ThreeBrainCoordinator(None, None, None)
        bus = _make_bus()
        await coord.subscribe_to_repository_events(bus)

        # Extract and invoke the registered handler
        _, handler = bus.subscribe.call_args[0]
        event = _make_event(added=["new.py"], updated=["mod.py"], removed=["del.py"])
        await handler(event)

        assert coord.stale_file_count == 3
        assert "new.py" in coord.stale_paths
        assert "mod.py" in coord.stale_paths
        assert "del.py" in coord.stale_paths

    async def test_multiple_events_accumulate_stale_paths(self):
        coord = ThreeBrainCoordinator(None, None, None)
        bus = _make_bus()
        await coord.subscribe_to_repository_events(bus)
        _, handler = bus.subscribe.call_args[0]

        await handler(_make_event(added=["a.py"], updated=[], removed=[]))
        await handler(_make_event(added=["b.py"], updated=[], removed=[]))
        assert coord.stale_file_count == 2

    async def test_clear_stale_resets_after_events(self):
        coord = ThreeBrainCoordinator(None, None, None)
        bus = _make_bus()
        await coord.subscribe_to_repository_events(bus)
        _, handler = bus.subscribe.call_args[0]
        await handler(_make_event(added=["a.py"], updated=[], removed=[]))
        coord.clear_stale()
        assert coord.stale_file_count == 0


# ---------------------------------------------------------------------------
# ThreeBrainCoordinator.query — all-None brains
# ---------------------------------------------------------------------------


class TestQueryAllNone:
    async def test_query_returns_result_when_all_brains_none(self):
        coord = ThreeBrainCoordinator(None, None, None)
        result = await coord.query("test query", "ses-1", "/workspace")
        assert isinstance(result, ThreeBrainResult)

    async def test_result_is_empty_when_all_brains_none(self):
        coord = ThreeBrainCoordinator(None, None, None)
        result = await coord.query("test query", "ses-1", "/workspace")
        assert result.is_empty()

    async def test_result_query_field_matches_input(self):
        coord = ThreeBrainCoordinator(None, None, None)
        result = await coord.query("auth flow", "ses-1", "/workspace")
        assert result.query == "auth flow"

    async def test_result_stale_count_zero_with_no_events(self):
        coord = ThreeBrainCoordinator(None, None, None)
        result = await coord.query("q", "ses-1", "/workspace")
        assert result.stale_file_count == 0


# ---------------------------------------------------------------------------
# ThreeBrainCoordinator.query — working brain
# ---------------------------------------------------------------------------


class TestQueryWorkingBrain:
    async def test_working_brain_hit_appears_in_result(self):
        turns = [_make_turn("user", "help me debug")]
        working = MagicMock()
        working.get_recent_turns = MagicMock(return_value=turns)

        coord = ThreeBrainCoordinator(working, None, None)
        result = await coord.query("debug", "ses-1", "/ws")
        assert result.working_hits == turns

    async def test_working_brain_uses_limit(self):
        working = MagicMock()
        working.get_recent_turns = MagicMock(return_value=[])

        coord = ThreeBrainCoordinator(working, None, None)
        await coord.query("q", "ses-1", "/ws", working_limit=3)
        working.get_recent_turns.assert_called_once_with(3)

    async def test_working_brain_exception_returns_empty(self):
        working = MagicMock()
        working.get_recent_turns = MagicMock(side_effect=RuntimeError("boom"))

        coord = ThreeBrainCoordinator(working, None, None)
        result = await coord.query("q", "ses-1", "/ws")
        assert result.working_hits == []


# ---------------------------------------------------------------------------
# ThreeBrainCoordinator.query — semantic brain
# ---------------------------------------------------------------------------


class TestQuerySemanticBrain:
    async def test_semantic_brain_hit_appears_in_result(self):
        hits = [_make_retrieved("past context")]
        semantic = MagicMock()
        semantic.search = AsyncMock(return_value=hits)

        coord = ThreeBrainCoordinator(None, semantic, None)
        result = await coord.query("context", "ses-1", "/ws")
        assert result.semantic_hits == hits

    async def test_semantic_brain_passes_workspace_and_limit(self):
        semantic = MagicMock()
        semantic.search = AsyncMock(return_value=[])

        coord = ThreeBrainCoordinator(None, semantic, None)
        await coord.query("q", "ses-1", "/myws", semantic_limit=7)
        semantic.search.assert_called_once_with("q", "/myws", limit=7)

    async def test_semantic_brain_exception_returns_empty(self):
        semantic = MagicMock()
        semantic.search = AsyncMock(side_effect=ValueError("oops"))

        coord = ThreeBrainCoordinator(None, semantic, None)
        result = await coord.query("q", "ses-1", "/ws")
        assert result.semantic_hits == []


# ---------------------------------------------------------------------------
# ThreeBrainCoordinator.query — episodic brain
# ---------------------------------------------------------------------------


class TestQueryEpisodicBrain:
    async def test_episodic_brain_hit_appears_in_result(self):
        turns = [_make_episodic_turn("assistant", "fixed the issue")]
        episodic = MagicMock()
        episodic.search_by_content = AsyncMock(return_value=turns)

        coord = ThreeBrainCoordinator(None, None, episodic)
        result = await coord.query("fix", "ses-1", "/ws")
        assert result.episodic_hits == turns

    async def test_episodic_brain_exception_returns_empty(self):
        episodic = MagicMock()
        episodic.search_by_content = AsyncMock(side_effect=ConnectionError("db down"))

        coord = ThreeBrainCoordinator(None, None, episodic)
        result = await coord.query("q", "ses-1", "/ws")
        assert result.episodic_hits == []


# ---------------------------------------------------------------------------
# ThreeBrainCoordinator.query — KG brain
# ---------------------------------------------------------------------------


class TestQueryKGBrain:
    async def test_kg_context_included_when_symbols_found(self):
        node = SimpleNamespace(
            node_type=SimpleNamespace(value="function"), label="my_func", file_path="app.py"
        )
        kg_query = MagicMock()
        kg_query.summary_text = AsyncMock(return_value="Graph: 10 files, 50 symbols.")
        kg_query.find_by_label = AsyncMock(return_value=[node])

        coord = ThreeBrainCoordinator(None, None, None, kg_query=kg_query)
        result = await coord.query("my_func", "ses-1", "/ws")
        assert result.kg_context is not None
        assert "my_func" in result.kg_context
        assert "Graph:" in result.kg_context

    async def test_kg_context_returns_summary_when_no_symbol_hits(self):
        kg_query = MagicMock()
        kg_query.summary_text = AsyncMock(return_value="Graph: 5 files, 10 symbols.")
        kg_query.find_by_label = AsyncMock(return_value=[])

        coord = ThreeBrainCoordinator(None, None, None, kg_query=kg_query)
        result = await coord.query("unknown_thing", "ses-1", "/ws")
        # No symbol hits → returns summary text
        assert result.kg_context == "Graph: 5 files, 10 symbols."

    async def test_kg_context_none_when_no_graph_available(self):
        kg_query = MagicMock()
        kg_query.summary_text = AsyncMock(return_value="No repository knowledge graph available.")
        kg_query.find_by_label = AsyncMock(return_value=[])

        coord = ThreeBrainCoordinator(None, None, None, kg_query=kg_query)
        result = await coord.query("q", "ses-1", "/ws")
        # "No repository" in summary → returns None
        assert result.kg_context is None

    async def test_kg_exception_returns_none(self):
        kg_query = MagicMock()
        kg_query.summary_text = AsyncMock(side_effect=OSError("db missing"))

        coord = ThreeBrainCoordinator(None, None, None, kg_query=kg_query)
        result = await coord.query("q", "ses-1", "/ws")
        assert result.kg_context is None


# ---------------------------------------------------------------------------
# Integration: all brains active
# ---------------------------------------------------------------------------


class TestQueryAllBrainsActive:
    async def test_all_brains_contribute_to_result(self):
        working_turns = [_make_turn("user", "current question")]
        semantic_hits = [_make_retrieved("past memory")]
        episodic_turns = [_make_episodic_turn("assistant", "old answer")]
        node = SimpleNamespace(
            node_type=SimpleNamespace(value="class"), label="MyClass", file_path="model.py"
        )

        working = MagicMock()
        working.get_recent_turns = MagicMock(return_value=working_turns)
        semantic = MagicMock()
        semantic.search = AsyncMock(return_value=semantic_hits)
        episodic = MagicMock()
        episodic.search_by_content = AsyncMock(return_value=episodic_turns)
        kg_query = MagicMock()
        kg_query.summary_text = AsyncMock(return_value="Graph: 100 files, 500 symbols.")
        kg_query.find_by_label = AsyncMock(return_value=[node])

        coord = ThreeBrainCoordinator(working, semantic, episodic, kg_query)
        result = await coord.query("MyClass", "ses-1", "/ws")

        assert result.working_hits == working_turns
        assert result.semantic_hits == semantic_hits
        assert result.episodic_hits == episodic_turns
        assert result.kg_context is not None
        assert result.total_hits() == 3

    async def test_stale_count_reflected_in_result(self):
        coord = ThreeBrainCoordinator(None, None, None)
        coord._stale_paths = {"a.py", "b.py", "c.py"}

        result = await coord.query("q", "ses-1", "/ws")
        assert result.stale_file_count == 3

    async def test_context_block_combines_all_sections(self):
        working_turns = [_make_turn("user", "current")]
        semantic_hits = [_make_retrieved("semantic")]
        episodic_turns = [_make_episodic_turn("user", "historical")]

        working = MagicMock()
        working.get_recent_turns = MagicMock(return_value=working_turns)
        semantic = MagicMock()
        semantic.search = AsyncMock(return_value=semantic_hits)
        episodic = MagicMock()
        episodic.search_by_content = AsyncMock(return_value=episodic_turns)

        coord = ThreeBrainCoordinator(working, semantic, episodic)
        result = await coord.query("test", "ses-1", "/ws")
        block = result.as_context_block()

        assert "Working Memory" in block
        assert "Semantic Memory" in block
        assert "Episodic Memory" in block
