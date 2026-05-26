"""Integration tests for EventBus to CognitiveBus migration (Batch 10)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from pathlib import Path
import pytest

from velune.kernel.bus import CognitiveBus
from velune.kernel.schemas import Event as KernelEvent
from velune.orchestration.engine import LangGraphOrchestrationEngine
from velune.orchestration.schemas import OrchestrationRequest
from velune.events.store.log import EventLog


@pytest.mark.asyncio
async def test_orchestration_engine_uses_cognitive_bus():
    """Verify that LangGraphOrchestrationEngine uses CognitiveBus to emit Pydantic KernelEvents."""
    event_bus = MagicMock(spec=CognitiveBus)
    event_bus.emit = AsyncMock()

    # Build engine with mocked parameters
    engine = LangGraphOrchestrationEngine(
        retrieval=MagicMock(),
        repository_cognition=MagicMock(),
        memory_lifecycle=MagicMock(),
        graph_memory=MagicMock(),
        tool_registry=MagicMock(),
        event_bus=event_bus,
    )

    # Mock _execute_nodes to prevent it from running the full multi-agent cycle
    engine._execute_nodes = AsyncMock(side_effect=lambda state, emit_fn: state)

    request = OrchestrationRequest(prompt="test prompt", workspace=".")
    await engine.execute_request(request)

    # Assert emit was called instead of publish
    assert not hasattr(event_bus, "publish") or not event_bus.publish.called
    assert event_bus.emit.called

    # Assert emitted event is a KernelEvent
    first_call_args = event_bus.emit.call_args_list[0][0]
    emitted_event = first_call_args[0]
    assert isinstance(emitted_event, KernelEvent)
    assert emitted_event.event_type == "orchestration.started"
    assert emitted_event.source == "orchestration"
    assert emitted_event.data["prompt"] == "test prompt"


@pytest.mark.asyncio
async def test_event_log_accepts_kernel_events(tmp_path: Path):
    """Verify EventLog appends and parses back KernelEvent instances correctly."""
    log_file = tmp_path / "events.jsonl"
    event_log = EventLog(log_file)

    event = KernelEvent(
        event_type="test.event",
        source="test_source",
        data={"key": "value"}
    )

    await event_log.append(event)

    # Read back
    events = await event_log.read_all()
    assert len(events) == 1
    read_event = events[0]
    assert isinstance(read_event, KernelEvent)
    assert read_event.event_type == "test.event"
    assert read_event.source == "test_source"
    assert read_event.data == {"key": "value"}


def test_no_old_eventbus_imports_remain():
    """Verify that after the deprecated files are deleted, import fails as expected."""
    import velune

    # After the migration cleanup, engine should not be importable
    with pytest.raises(ImportError):
        import velune.events.bus.engine
