"""Tests for velune.cli.handlers.prompt_context.build_turn_context.

Covers the Phase 1 architectural-convergence wiring: IntentClassifier bias,
ThreeBrainCoordinator fan-out (replacing the old ad-hoc HybridRetriever-only
call), the cached Repository Brain snapshot/drift participation, lineage
continuity, and the working_hits double-count regression guard.

Supersedes the old tests/test_retrieval_semantic_context.py, whose target
(``VeluneREPL._retrieve_semantic_context``) no longer exists — that logic now
lives in ``_retrieve_hybrid`` below.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from velune.cli.handlers.prompt_context import (
    _repository_snapshot_chunks,
    _retrieve_hybrid,
    _retrieve_via_memory_lifecycle,
    build_turn_context,
)
from velune.cli.modes import ModeConfig, ModeManager, SessionMode
from velune.cognition.intent import IntentType
from velune.context.sections import ContextSection
from velune.memory.lifecycle import RetrievedContext, RetrievedResult
from velune.retrieval.schemas import (
    RetrievalDocument,
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    RetrievalSource,
)


def _mode_config(**overrides) -> ModeConfig:
    base = {
        "mode": SessionMode.NORMAL,
        "council_tier": "auto",
        "context_compression": False,
        "max_context_tokens": 16384,
        "temperature": 0.3,
        "retrieval_depth": 8,
        "use_fastest_model": False,
        "use_largest_model": False,
        "disable_critics": False,
        "description": "test",
        "prompt_color": "cyan",
    }
    base.update(overrides)
    return ModeConfig(**base)


def _make_repl(container_map: dict, conversation: list | None = None, mode_config=None):
    repl = MagicMock()
    repl.container.get.side_effect = lambda key: container_map.get(key)
    repl._conversation = (
        conversation
        if conversation is not None
        else [
            {"role": "user", "content": "hello there"},
        ]
    )
    repl._episodic_session_id = "ses-test"
    mode_manager = MagicMock(spec=ModeManager)
    mode_manager.current = SessionMode.NORMAL
    mode_manager.config = mode_config or _mode_config()
    repl._mode_manager = mode_manager
    return repl


def _model(model_id: str = "test-model", context_length: int = 8192):
    return SimpleNamespace(model_id=model_id, context_length=context_length)


# ---------------------------------------------------------------------------
# _retrieve_hybrid — replaces old _retrieve_semantic_context tests
# ---------------------------------------------------------------------------


async def test_retrieve_hybrid_calls_retrieve_with_a_real_query():
    retrieval = MagicMock()
    hit = RetrievalHit(
        document=RetrievalDocument(id="a.py", content="def a(): pass"),
        score=0.9,
        source=RetrievalSource.VECTOR,
        rank=1,
    )
    retrieval.retrieve = AsyncMock(
        return_value=RetrievalResult(query=RetrievalQuery(text="x"), hits=[hit])
    )
    repl = _make_repl({"runtime.retrieval": retrieval})

    chunks = await _retrieve_hybrid(
        repl, "how does auth work", top_k=3, intent=IntentType.QUESTION, confidence=0.8
    )

    retrieval.retrieve.assert_awaited_once()
    (call_query,), _ = retrieval.retrieve.call_args
    assert isinstance(call_query, RetrievalQuery)
    assert call_query.text == "how does auth work"
    # top_k on the query itself comes from the planner's intent strategy, not
    # the caller's depth — the caller's top_k caps the *final* chunk count
    # (checked below) so the reranker still sees the planner's full candidate
    # pool before truncation.
    assert call_query.intent == IntentType.QUESTION.value
    assert len(chunks) == 1
    assert chunks[0].section == ContextSection.RETRIEVED_CONTEXT
    assert "def a(): pass" in chunks[0].content


async def test_retrieve_hybrid_caps_chunks_at_callers_top_k():
    retrieval = MagicMock()
    hits = [
        RetrievalHit(
            document=RetrievalDocument(id=f"{i}.py", content=f"content {i}"),
            score=0.9,
            source=RetrievalSource.VECTOR,
            rank=i,
        )
        for i in range(5)
    ]
    retrieval.retrieve = AsyncMock(
        return_value=RetrievalResult(query=RetrievalQuery(text="x"), hits=hits)
    )
    repl = _make_repl({"runtime.retrieval": retrieval})

    chunks = await _retrieve_hybrid(
        repl, "find the sandbox validator", top_k=2, intent=IntentType.SEARCH, confidence=0.9
    )

    assert len(chunks) == 2


async def test_retrieve_hybrid_returns_empty_on_no_hits():
    retrieval = MagicMock()
    retrieval.retrieve = AsyncMock(
        return_value=RetrievalResult(query=RetrievalQuery(text="x"), hits=[])
    )
    repl = _make_repl({"runtime.retrieval": retrieval})
    assert (
        await _retrieve_hybrid(
            repl, "nothing relevant", top_k=3, intent=IntentType.QUESTION, confidence=0.5
        )
        == []
    )


async def test_retrieve_hybrid_degrades_silently_when_retrieval_missing():
    repl = _make_repl({"runtime.retrieval": None})
    assert (
        await _retrieve_hybrid(
            repl, "anything", top_k=3, intent=IntentType.QUESTION, confidence=0.5
        )
        == []
    )


async def test_retrieve_hybrid_swallows_retriever_errors():
    retrieval = MagicMock()
    retrieval.retrieve = AsyncMock(side_effect=RuntimeError("boom"))
    repl = _make_repl({"runtime.retrieval": retrieval})
    assert (
        await _retrieve_hybrid(
            repl, "anything", top_k=3, intent=IntentType.QUESTION, confidence=0.5
        )
        == []
    )


async def test_retrieve_hybrid_clips_out_of_range_scores_to_valid_trust_score():
    retrieval = MagicMock()
    hit = RetrievalHit(
        document=RetrievalDocument(id="a.py", content="content"),
        score=5.0,  # out of the [0, 1] range ContextChunk requires
        source=RetrievalSource.LEXICAL,
        rank=1,
    )
    retrieval.retrieve = AsyncMock(
        return_value=RetrievalResult(query=RetrievalQuery(text="x"), hits=[hit])
    )
    repl = _make_repl({"runtime.retrieval": retrieval})
    chunks = await _retrieve_hybrid(repl, "q", top_k=3, intent=IntentType.QUESTION, confidence=0.5)
    assert chunks[0].trust_score == 1.0


# ---------------------------------------------------------------------------
# _retrieve_via_memory_lifecycle — vitality-aware retrieval, working_hits
# double-count regression guard
# ---------------------------------------------------------------------------


async def test_memory_lifecycle_drops_working_results_to_avoid_double_count():
    """repl._conversation already supplies WORKING_MEMORY directly; once
    record_turn() starts filling runtime.working_memory too, converting a
    "working" result into a chunk here would double-count the same turns."""
    manager = MagicMock()
    manager.retrieve = AsyncMock(
        return_value=RetrievedContext(
            results=[
                RetrievedResult(content="should never appear", source_type="working"),
            ]
        )
    )
    repl = _make_repl({"runtime.memory_lifecycle": manager})
    chunks = await _retrieve_via_memory_lifecycle(
        repl, "q", workspace=MagicMock(__str__=lambda s: "/ws"), depth=5, budget_tokens=2000
    )
    assert chunks == []


async def test_memory_lifecycle_converts_semantic_episodic_and_kg_results():
    manager = MagicMock()
    manager.retrieve = AsyncMock(
        return_value=RetrievedContext(
            results=[
                RetrievedResult(
                    content="semantic memory", source_type="semantic", trust_score=0.8
                ),
                RetrievedResult(content="episodic memory", source_type="episodic"),
                RetrievedResult(content="Graph: 10 files", source_type="kg"),
            ]
        )
    )
    repl = _make_repl({"runtime.memory_lifecycle": manager})
    chunks = await _retrieve_via_memory_lifecycle(
        repl, "q", workspace=MagicMock(__str__=lambda s: "/ws"), depth=5, budget_tokens=2000
    )

    sources = {c.source for c in chunks}
    assert sources == {"semantic_memory", "episodic_memory", "knowledge_graph"}
    assert all(c.section == ContextSection.RETRIEVED_CONTEXT for c in chunks)


async def test_memory_lifecycle_carries_vitality_into_chunk_metadata():
    """The whole point of routing through MemoryLifecycleManager instead of
    querying ThreeBrainCoordinator directly: a stale hit's vitality
    classification (and its trust decay) must survive into the chunk."""
    manager = MagicMock()
    manager.retrieve = AsyncMock(
        return_value=RetrievedContext(
            results=[
                RetrievedResult(
                    content="old note",
                    source_type="semantic",
                    trust_score=0.2,
                    vitality="archived",
                    attribution="32 days ago",
                ),
            ]
        )
    )
    repl = _make_repl({"runtime.memory_lifecycle": manager})
    chunks = await _retrieve_via_memory_lifecycle(
        repl, "q", workspace=MagicMock(__str__=lambda s: "/ws"), depth=5, budget_tokens=2000
    )

    assert len(chunks) == 1
    assert chunks[0].trust_score == 0.2
    assert chunks[0].metadata["vitality"] == "archived"
    assert chunks[0].metadata["attribution"] == "32 days ago"


async def test_memory_lifecycle_passes_budget_and_depth_through():
    manager = MagicMock()
    manager.retrieve = AsyncMock(return_value=RetrievedContext())
    repl = _make_repl({"runtime.memory_lifecycle": manager})

    await _retrieve_via_memory_lifecycle(
        repl, "q", workspace=MagicMock(__str__=lambda s: "/ws"), depth=7, budget_tokens=3000
    )

    manager.retrieve.assert_awaited_once()
    _, kwargs = manager.retrieve.call_args
    assert kwargs["depth"] == 7
    assert kwargs["budget"] == 3000


async def test_memory_lifecycle_degrades_silently_when_manager_missing():
    repl = _make_repl({"runtime.memory_lifecycle": None})
    assert (
        await _retrieve_via_memory_lifecycle(
            repl, "q", workspace=MagicMock(__str__=lambda s: "/ws"), depth=5, budget_tokens=2000
        )
        == []
    )


async def test_memory_lifecycle_swallows_retrieve_errors():
    manager = MagicMock()
    manager.retrieve = AsyncMock(side_effect=RuntimeError("boom"))
    repl = _make_repl({"runtime.memory_lifecycle": manager})
    assert (
        await _retrieve_via_memory_lifecycle(
            repl, "q", workspace=MagicMock(__str__=lambda s: "/ws"), depth=5, budget_tokens=2000
        )
        == []
    )


# ---------------------------------------------------------------------------
# _repository_snapshot_chunks — cheap cached snapshot, not .index()
# ---------------------------------------------------------------------------


async def test_repository_snapshot_absent_on_cold_start():
    repo_service = MagicMock()
    repo_service.get_snapshot_fresh = AsyncMock(return_value=None)
    repl = _make_repl({"runtime.repository_cognition": repo_service})
    assert await _repository_snapshot_chunks(repl, 2500) == []


async def test_repository_snapshot_present_when_cache_exists():
    repo_service = MagicMock()
    # The fast turn path reads the incrementally-maintained pipeline cache via
    # get_snapshot_fresh(), which carries dependency edges + api_map.
    repo_service.get_snapshot_fresh = AsyncMock(return_value=MagicMock(api_map=None))
    repl = _make_repl({"runtime.repository_cognition": repo_service, "runtime.firewall": None})

    with patch(
        "velune.repository.context_builder.WorkspaceContextBuilder.build",
        return_value=("[WORKSPACE: /repo]", None),
    ):
        chunks = await _repository_snapshot_chunks(repl, 2500)

    assert len(chunks) == 1
    assert chunks[0].section == ContextSection.REPOSITORY_SNAPSHOT
    assert "UNTRUSTED WORKSPACE CONTENT" in chunks[0].content
    assert "[WORKSPACE: /repo]" in chunks[0].content


async def test_repository_drift_chunk_present_when_violations_exist():
    repo_service = MagicMock()
    repo_service.get_snapshot_fresh = AsyncMock(return_value=MagicMock(api_map=None))
    repl = _make_repl({"runtime.repository_cognition": repo_service, "runtime.firewall": None})

    with patch(
        "velune.repository.context_builder.WorkspaceContextBuilder.build",
        return_value=("[WORKSPACE: /repo]", "[ARCHITECTURE VIOLATIONS — 1 issue(s) detected]"),
    ):
        chunks = await _repository_snapshot_chunks(repl, 2500)

    sections = {c.section for c in chunks}
    assert ContextSection.REPOSITORY_SNAPSHOT in sections
    assert ContextSection.ARCHITECTURAL_DRIFT in sections


async def test_repository_snapshot_degrades_silently_when_service_missing():
    repl = _make_repl({"runtime.repository_cognition": None})
    assert await _repository_snapshot_chunks(repl, 2500) == []


# ---------------------------------------------------------------------------
# build_turn_context — end-to-end intent bias and section assembly
# ---------------------------------------------------------------------------


def _container_map(**overrides):
    base = {
        "runtime.workspace": "/ws",
        "runtime.retrieval": None,
        "runtime.memory_lifecycle": None,
        "runtime.repository_cognition": None,
        "runtime.firewall": None,
    }
    base.update(overrides)
    return base


async def test_fresh_repo_first_prompt_does_not_crash():
    repl = _make_repl(_container_map())
    messages, report, intent, confidence = await build_turn_context(repl, "hello", _model())

    assert messages[-1] == {"role": "user", "content": "hello"}
    assert report.total_chunks_received >= 2  # SYSTEM_PROMPT + CURRENT_PROMPT at minimum
    assert not report.budget_exceeded


async def test_debug_intent_biases_toward_deeper_retrieval_than_generate():
    manager = MagicMock()
    manager.retrieve = AsyncMock(return_value=RetrievedContext())
    repl = _make_repl(_container_map(**{"runtime.memory_lifecycle": manager}))

    await build_turn_context(repl, "Traceback (most recent call last): KeyError: 'x'", _model())
    debug_call = manager.retrieve.call_args
    manager.retrieve.reset_mock()

    await build_turn_context(repl, "write a new function to add two numbers", _model())
    generate_call = manager.retrieve.call_args

    assert debug_call.kwargs["depth"] > generate_call.kwargs["depth"]


async def test_refactor_intent_biases_repository_snapshot_budget():
    repo_service = MagicMock()
    repo_service.get_snapshot_fresh = AsyncMock(return_value=MagicMock(api_map=None))
    repl = _make_repl(_container_map(**{"runtime.repository_cognition": repo_service}))

    captured_budgets: list[int] = []

    def _fake_build(self, snapshot, delta=None, max_snapshot_tokens=2500, api_map=None):
        captured_budgets.append(max_snapshot_tokens)
        return "[WORKSPACE]", None

    with patch(
        "velune.repository.context_builder.WorkspaceContextBuilder.build",
        _fake_build,
    ):
        await build_turn_context(repl, "refactor this messy module", _model())
        await build_turn_context(repl, "write a new helper function", _model())

    assert captured_budgets[0] > captured_budgets[1]


async def test_retrieval_depth_from_mode_flows_into_memory_lifecycle_retrieve():
    manager = MagicMock()
    manager.retrieve = AsyncMock(return_value=RetrievedContext())
    repl = _make_repl(
        _container_map(**{"runtime.memory_lifecycle": manager}),
        mode_config=_mode_config(retrieval_depth=20),
    )

    await build_turn_context(repl, "what does this function do?", _model())  # EXPLAIN → full depth
    assert manager.retrieve.call_args.kwargs["depth"] == 20


async def test_compression_runs_only_when_mode_enables_it():
    conversation = [{"role": "user", "content": "x" * 5000}]
    repl = _make_repl(
        _container_map(),
        conversation=list(conversation),
        mode_config=_mode_config(context_compression=True, max_context_tokens=100),
    )

    with patch("velune.context.extractive.compress_conversation", return_value=[]) as compress:
        await build_turn_context(repl, "hi", _model())
    compress.assert_called_once()


async def test_no_compression_when_mode_disables_it():
    repl = _make_repl(_container_map(), mode_config=_mode_config(context_compression=False))
    with patch("velune.context.extractive.compress_conversation") as compress:
        await build_turn_context(repl, "hi", _model())
    compress.assert_not_called()


async def test_working_memory_excludes_duplicate_current_turn():
    """The just-appended user turn must not appear twice (once as WORKING_MEMORY,
    once as CURRENT_PROMPT)."""
    repl = _make_repl(
        _container_map(),
        conversation=[
            {"role": "user", "content": "earlier turn"},
            {"role": "assistant", "content": "earlier reply"},
            {"role": "user", "content": "current turn"},
        ],
    )
    messages, report, _, _ = await build_turn_context(repl, "current turn", _model())
    assembled = messages[0]["content"]
    assert assembled.count("current turn") == 1
