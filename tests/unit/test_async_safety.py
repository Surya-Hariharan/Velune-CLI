"""Unit tests for Python 3.12+ async safety remediation (Batch 06)."""

from __future__ import annotations

import asyncio
import sys
import warnings
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from rich.console import Console

from velune.cli.app import _show_startup_animation
from velune.kernel.bus import CognitiveBus
from velune.kernel.schemas import Event as KernelEvent
from velune.retrieval.hybrid import HybridRetriever
from velune.retrieval.schemas import RetrievalQuery, RetrievalResult


@pytest.mark.asyncio
async def test_cognitive_bus_emit_and_wait_uses_running_loop():
    """Verify that emit_and_wait() does not raise DeprecationWarning for event loop selection."""
    bus = CognitiveBus()
    event = KernelEvent(
        event_type="test_event",
        source="test",
        data={}
    )

    async def trigger_response():
        # Let the emit_and_wait call start and register the future
        await asyncio.sleep(0.02)
        response_event = KernelEvent(
            event_type="response_event",
            source="test",
            correlation_id=event.event_id,
            data={}
        )
        await bus.emit(response_event)

    asyncio.create_task(trigger_response())

    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        
        response = await bus.emit_and_wait(event, timeout=1.0)
        
        # Verify response is returned correctly
        assert response.correlation_id == event.event_id

        # Verify no asyncio deprecation warnings regarding get_event_loop are raised
        asyncio_warnings = [
            w for w in recorded
            if issubclass(w.category, DeprecationWarning)
            and "get_event_loop" in str(w.message)
        ]
        assert len(asyncio_warnings) == 0, f"Found asyncio deprecation warnings: {asyncio_warnings}"


@pytest.mark.asyncio
async def test_search_sync_raises_from_async_context():
    """Verify that HybridRetriever.search_sync() raises RuntimeError inside a running event loop."""
    retriever = HybridRetriever()
    query = RetrievalQuery(text="test query")

    with pytest.raises(RuntimeError) as exc_info:
        retriever.search_sync(query)

    assert "cannot be called from an async context" in str(exc_info.value)


def test_search_sync_works_from_sync_context():
    """Verify search_sync() works correctly outside of an active event loop, issuing a DeprecationWarning."""
    retriever = HybridRetriever()
    query = RetrievalQuery(text="test query")

    expected_result = RetrievalResult(
        query=query,
        hits=[],
        strategy="mocked"
    )
    retriever.retrieve = AsyncMock(return_value=expected_result)

    with pytest.warns(DeprecationWarning) as record:
        res = retriever.search_sync(query)

    assert res == expected_result
    # Verify the deprecation warning was issued
    assert any("search_sync() is deprecated" in str(w.message) for w in record)


def test_animation_skipped_in_non_tty():
    """Verify _show_startup_animation skips processing if sys.stdout is not a TTY."""
    console = MagicMock(spec=Console)
    workspace = Path("/mock/workspace")

    with patch("sys.stdout.isatty", return_value=False), \
         patch("velune.cli.app.Live") as mock_live:

        _show_startup_animation(console, workspace, None)

        # rich.Live should not be instantiated at all
        mock_live.assert_not_called()
