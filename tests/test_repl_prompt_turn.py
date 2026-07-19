"""Tests for the REPL-level Phase 1 architectural-convergence wiring:

- _start_episodic_session / _end_episodic_session call EpisodicMemory with the
  corrected argument shapes (the old mismatched kwargs raised TypeError on
  every call, silently swallowed by a debug-level except).
- _record_turn_async fires MemoryLifecycleManager.record_turn() for a turn.
- _emit_turn_events carries the enriched intent/context_report/three_brain/
  repository_brain payload so `velune trace` can see architectural
  regressions, not just user/response text.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from velune.cli.modes import SessionMode
from velune.cli.repl import VeluneREPL
from velune.context.sections import ContextAssemblyReport, ContextSection


def _make_repl() -> VeluneREPL:
    repl = VeluneREPL.__new__(VeluneREPL)  # bypass heavyweight __init__
    repl.container = MagicMock()
    repl._episodic_session_id = None
    repl._mode_manager = MagicMock()
    repl._mode_manager.current = SessionMode.NORMAL
    repl.active_model = MagicMock(model_id="test-model")
    return repl


# ---------------------------------------------------------------------------
# Episodic session lifecycle — signature-mismatch bug fix
# ---------------------------------------------------------------------------


async def test_start_episodic_session_uses_corrected_argument_shape():
    episodic = MagicMock()
    episodic.start_session = AsyncMock(return_value="ses-123")
    repl = _make_repl()
    repl.container.get.side_effect = lambda key: {
        "runtime.episodic_session_memory": episodic,
        "runtime.workspace": "/ws",
    }.get(key)

    await repl._start_episodic_session()

    episodic.start_session.assert_awaited_once_with(
        workspace_root="/ws", model="test-model", mode="normal"
    )
    assert repl._episodic_session_id == "ses-123"


async def test_start_episodic_session_does_not_raise_on_backend_error():
    episodic = MagicMock()
    episodic.start_session = AsyncMock(side_effect=RuntimeError("db down"))
    repl = _make_repl()
    repl.container.get.side_effect = lambda key: {
        "runtime.episodic_session_memory": episodic,
        "runtime.workspace": "/ws",
    }.get(key)

    await repl._start_episodic_session()  # must not raise
    assert repl._episodic_session_id is None


async def test_end_episodic_session_uses_corrected_argument_shape():
    episodic = MagicMock()
    episodic.end_session = AsyncMock()
    repl = _make_repl()
    repl._episodic_session_id = "ses-123"
    repl.container.get.side_effect = lambda key: {
        "runtime.episodic_session_memory": episodic,
    }.get(key)

    await repl._end_episodic_session()

    episodic.end_session.assert_awaited_once_with("ses-123")
    assert repl._episodic_session_id is None


async def test_end_episodic_session_is_noop_without_active_session():
    episodic = MagicMock()
    episodic.end_session = AsyncMock()
    repl = _make_repl()
    repl._episodic_session_id = None
    repl.container.get.side_effect = lambda key: {
        "runtime.episodic_session_memory": episodic,
    }.get(key)

    await repl._end_episodic_session()
    episodic.end_session.assert_not_awaited()


# ---------------------------------------------------------------------------
# _record_turn_async — fire-and-forget MemoryLifecycleManager.record_turn()
# ---------------------------------------------------------------------------


async def test_record_turn_async_calls_memory_lifecycle_record_turn():
    manager = MagicMock()
    manager.record_turn = AsyncMock(return_value="trn-1")
    repl = _make_repl()
    repl._episodic_session_id = "ses-1"
    repl.container.get.side_effect = lambda key: {
        "runtime.memory_lifecycle": manager,
    }.get(key)

    repl._record_turn_async(
        role="user", content="hello", model_id="m1", workspace_root="/ws", tokens=5
    )
    await asyncio.sleep(0)  # let the fire-and-forget task run

    manager.record_turn.assert_awaited_once_with(
        session_id="ses-1", role="user", content="hello", model="m1", tokens=5,
        workspace_root="/ws",
    )


async def test_record_turn_async_degrades_silently_when_manager_missing():
    repl = _make_repl()
    repl.container.get.side_effect = lambda key: None
    repl._record_turn_async(role="user", content="hi", model_id="m1", workspace_root="/ws")
    await asyncio.sleep(0)  # must not raise


# ---------------------------------------------------------------------------
# _emit_turn_events — enriched observability payload
# ---------------------------------------------------------------------------


async def test_emit_turn_events_includes_intent_and_context_report():
    bus = MagicMock()
    bus.emit = AsyncMock()
    repl = _make_repl()
    repl.container.get.side_effect = lambda key: {"runtime.bus": bus}.get(key)

    report = ContextAssemblyReport(
        total_chunks_received=4,
        total_tokens_requested=1000,
        total_tokens_assembled=500,
        sections_present=[ContextSection.REPOSITORY_SNAPSHOT, ContextSection.RETRIEVED_CONTEXT],
    )

    await repl._emit_turn_events(
        "hello",
        "hi there",
        "test-model",
        42,
        intent="debug",
        intent_confidence=0.8,
        context_report=report,
    )

    bus.emit.assert_awaited_once()
    (event,), _ = bus.emit.call_args
    assert event.event_type == "turn.completed"
    assert event.data["intent"] == "debug"
    assert event.data["intent_confidence"] == 0.8
    assert event.data["context_report"]["total_chunks_received"] == 4
    assert event.data["repository_brain"]["snapshot_present"] is True
    assert event.data["repository_brain"]["drift_present"] is False
    assert event.data["three_brain"]["retrieved_context_present"] is True


async def test_emit_turn_events_still_works_without_enrichment_fields():
    """Backward-compatible: callers that don't pass intent/context_report
    (e.g. an earlier code path) still get a valid turn.completed event."""
    bus = MagicMock()
    bus.emit = AsyncMock()
    repl = _make_repl()
    repl.container.get.side_effect = lambda key: {"runtime.bus": bus}.get(key)

    await repl._emit_turn_events("hello", "hi", "test-model", 10)

    bus.emit.assert_awaited_once()
    (event,), _ = bus.emit.call_args
    assert "intent" not in event.data
    assert "context_report" not in event.data


async def test_emit_turn_events_swallows_bus_errors():
    repl = _make_repl()
    repl.container.get.side_effect = RuntimeError("no bus")
    await repl._emit_turn_events("hello", "hi", "test-model", 10)  # must not raise
