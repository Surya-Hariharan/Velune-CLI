"""Tests for velune.memory.lifecycle.MemoryLifecycleManager.

Covers the Phase 1 architectural-convergence changes:
- retrieve() delegates its multi-tier fan-out to a single ThreeBrainCoordinator
  instance instead of reimplementing working/episodic/semantic queries, while
  preserving the RetrievedContext/RetrievedResult shape velune/mcp/server.py
  depends on.
- record_turn() still writes every tier and triggers the compaction check.
- The compaction provider bug (_check_and_trigger_compaction hardcoded
  provider=None, so ContextCompactor could never summarize) is fixed.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from velune.memory.lifecycle import MemoryLifecycleManager, RetrievedContext
from velune.memory.three_brain import ThreeBrainResult


def _manager(**overrides) -> MemoryLifecycleManager:
    defaults = {
        "working_tier": MagicMock(),
        "episodic_memory": MagicMock(),
        "semantic_memory": MagicMock(),
        "embedding_pipeline": MagicMock(),
        "lineage_tier": MagicMock(),
    }
    defaults.update(overrides)
    return MemoryLifecycleManager(**defaults)


# ---------------------------------------------------------------------------
# retrieve() — delegates to the shared ThreeBrainCoordinator
# ---------------------------------------------------------------------------


async def test_retrieve_delegates_to_injected_three_brain_coordinator():
    coordinator = MagicMock()
    coordinator.query = AsyncMock(return_value=ThreeBrainResult())
    manager = _manager(three_brain=coordinator)

    await manager.retrieve("auth flow", "/workspace", budget=4000)

    coordinator.query.assert_awaited_once()
    args, kwargs = coordinator.query.call_args
    assert args[0] == "auth flow"
    assert kwargs["session_id"] == "/workspace"
    assert kwargs["workspace_root"] == "/workspace"


async def test_retrieve_uses_default_session_id_when_workspace_root_empty():
    coordinator = MagicMock()
    coordinator.query = AsyncMock(return_value=ThreeBrainResult())
    manager = _manager(three_brain=coordinator)

    await manager.retrieve("q", "", budget=4000)
    assert coordinator.query.call_args.kwargs["session_id"] == "default"


async def test_retrieve_preserves_result_shape_for_mcp_server_contract():
    """velune/mcp/server.py reads .content/.source_type/.relevance_score/.attribution
    off each RetrievedResult — this shape must survive the delegation rewrite."""
    coordinator = MagicMock()
    coordinator.query = AsyncMock(
        return_value=ThreeBrainResult(
            working_hits=[
                SimpleNamespace(content="working turn", session_id="s1", timestamp=0.0)
            ],
            episodic_hits=[
                SimpleNamespace(
                    content="episodic turn", session_id="s1", created_at=0.0
                )
            ],
            semantic_hits=[
                SimpleNamespace(
                    content="semantic hit",
                    distance=0.1,
                    trust_score=0.9,
                    session_id="s1",
                    age_seconds=10.0,
                    attribution="just now",
                )
            ],
            kg_context="Graph: 5 files",
        )
    )
    manager = _manager(three_brain=coordinator)

    context = await manager.retrieve("q", "/ws", budget=4000)

    assert isinstance(context, RetrievedContext)
    source_types = {r.source_type for r in context.results}
    assert source_types == {"working", "episodic", "semantic", "kg"}
    for result in context.results:
        assert isinstance(result.content, str) and result.content
        assert isinstance(result.relevance_score, float)
        assert isinstance(result.attribution, str)


async def test_retrieve_ranks_results_by_relevance_times_trust():
    coordinator = MagicMock()
    coordinator.query = AsyncMock(
        return_value=ThreeBrainResult(
            semantic_hits=[
                SimpleNamespace(
                    content="low trust",
                    distance=0.0,  # relevance 1.0
                    trust_score=0.1,
                    session_id="s1",
                    age_seconds=3000000.0,  # > 30 days -> archived, trust *= 0.2
                    attribution="a month ago",
                ),
            ],
            episodic_hits=[
                SimpleNamespace(content="recent episodic", session_id="s1", created_at=0.0),
            ],
        )
    )
    manager = _manager(three_brain=coordinator)
    context = await manager.retrieve("q", "/ws", budget=4000)

    # Episodic (relevance 0.7 * trust 0.8 = 0.56) should outrank the
    # heavily-discounted archived semantic hit (1.0 * 0.1 * 0.2 = 0.02).
    assert context.results[0].source_type == "episodic"


async def test_retrieve_degrades_gracefully_when_coordinator_query_raises():
    coordinator = MagicMock()
    coordinator.query = AsyncMock(side_effect=RuntimeError("boom"))
    manager = _manager(three_brain=coordinator)

    context = await manager.retrieve("q", "/ws", budget=4000)
    assert context.results == []


async def test_retrieve_lazily_builds_coordinator_when_not_injected():
    """If no three_brain is passed at construction (e.g. a test builds the
    manager directly), retrieve() must still work by lazily constructing one
    from the manager's own tiers rather than reimplementing the fan-out."""
    working = MagicMock()
    working.get_recent_turns = MagicMock(return_value=[])
    manager = _manager(working_tier=working, semantic_memory=None, episodic_memory=None)

    context = await manager.retrieve("q", "/ws", budget=4000)
    assert isinstance(context, RetrievedContext)
    assert manager._three_brain_coordinator is not None


# ---------------------------------------------------------------------------
# record_turn() — writes every tier and triggers the compaction check
# ---------------------------------------------------------------------------


async def test_record_turn_writes_episodic_semantic_and_working():
    episodic = MagicMock()
    episodic.record_turn = AsyncMock(return_value="trn-1")
    semantic = MagicMock()
    working = MagicMock()
    manager = _manager(episodic_memory=episodic, semantic_memory=semantic, working_tier=working)

    with patch.object(
        MemoryLifecycleManager, "_check_and_trigger_compaction", new=AsyncMock()
    ) as check:
        turn_id = await manager.record_turn(
            session_id="ses-1", role="user", content="hello", model="m1", tokens=10
        )

    assert turn_id == "trn-1"
    episodic.record_turn.assert_awaited_once_with(
        session_id="ses-1", role="user", content="hello", model="m1", tokens=10
    )
    semantic.index_turn.assert_called_once()
    working.add_turn.assert_called_once_with("user", "hello", {"model": "m1", "tokens": 10})
    check.assert_awaited_once_with("ses-1")


async def test_record_turn_skips_semantic_index_when_episodic_write_failed():
    episodic = MagicMock()
    episodic.record_turn = AsyncMock(return_value="")  # write failed, no turn_id
    semantic = MagicMock()
    manager = _manager(episodic_memory=episodic, semantic_memory=semantic)

    with patch.object(MemoryLifecycleManager, "_check_and_trigger_compaction", new=AsyncMock()):
        await manager.record_turn(session_id="ses-1", role="user", content="hello")

    semantic.index_turn.assert_not_called()


# ---------------------------------------------------------------------------
# Compaction provider bug fix: _check_and_trigger_compaction must resolve a
# real provider, not the hardcoded `None` that silently broke every
# summarization attempt.
# ---------------------------------------------------------------------------


def test_resolve_compaction_provider_prefers_ollama():
    fake_ollama = MagicMock()
    registry = MagicMock()
    registry.get.side_effect = lambda name: fake_ollama if name == "ollama" else None
    manager = _manager(provider_registry=registry)

    assert manager._resolve_compaction_provider() is fake_ollama


def test_resolve_compaction_provider_falls_back_to_any_available_provider():
    fake_openai = MagicMock()
    registry = MagicMock()
    registry.get.side_effect = lambda name: fake_openai if name == "openai" else None
    registry.list_available_providers.return_value = ["openai"]
    manager = _manager(provider_registry=registry)

    assert manager._resolve_compaction_provider() is fake_openai


def test_resolve_compaction_provider_returns_none_without_registry():
    manager = _manager(provider_registry=None)
    assert manager._resolve_compaction_provider() is None


def test_resolve_compaction_provider_swallows_registry_errors():
    registry = MagicMock()
    registry.get.side_effect = RuntimeError("boom")
    manager = _manager(provider_registry=registry)
    assert manager._resolve_compaction_provider() is None


async def test_check_and_trigger_compaction_constructs_compactor_with_real_provider():
    """Regression guard for the bug where ContextCompactor was always built
    with provider=None, so _generate_summary always failed silently."""
    working = MagicMock()
    working.get_turns.return_value = []
    fake_provider = MagicMock()
    registry = MagicMock()
    registry.get.side_effect = lambda name: fake_provider if name == "ollama" else None
    manager = _manager(working_tier=working, provider_registry=registry)

    with patch("velune.memory.compaction.ContextCompactor") as compactor_cls:
        compactor_cls.return_value.should_compact = AsyncMock(return_value=False)
        await manager._check_and_trigger_compaction("ses-1")

    _, kwargs = compactor_cls.call_args
    assert kwargs["provider"] is fake_provider
