"""Bridge the live :class:`~velune.events.CognitiveBus` into a persistent trace.

A :class:`TraceSink` subscribes to *every* event on the bus and writes each one
to a :class:`~velune.observability.trace_log.TraceLog`, so the real execution
stream survives the process and can be replayed by ``velune trace``. It records
only what is actually emitted — it never synthesizes events, reasoning, or
prompts.

The sink is deliberately passive and failure-isolated: a write error is
swallowed so observability can never break the run it observes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from velune.observability.trace_log import TRACE_FILENAME, TraceLog

if TYPE_CHECKING:
    from velune.events import CognitiveBus, Event, Subscription

logger = logging.getLogger("velune.observability.trace_sink")


def trace_log_for_workspace(workspace: Path) -> TraceLog:
    """Return the :class:`TraceLog` for *workspace* in non-synced app storage."""
    from velune.core.paths import workspace_storage_dir

    return TraceLog(workspace_storage_dir(workspace) / TRACE_FILENAME)


def record_milestone(log: TraceLog, run_id: str, seq: int, phase: str, message: str) -> None:
    """Persist one orchestration milestone as a trace event.

    Shared by the orchestrator's progress callback so the event shape stored for
    a streamed phase is defined (and tested) in exactly one place. ``phase`` is
    the lowercased council stage (``planner``, ``coder``, …) or empty for an
    unlabeled progress line, in which case the event is tagged ``council``.
    """
    import time

    stage = phase or "council"
    log.append(
        {
            "event_id": f"{run_id}-{seq}",
            "event_type": f"council.{phase or 'progress'}",
            "timestamp": time.time(),
            "source": stage,
            "correlation_id": run_id,
            "data": {"message": message},
        }
    )


class TraceSink:
    """Persist every bus event to a :class:`TraceLog`."""

    def __init__(self, log: TraceLog) -> None:
        self._log = log
        self._sub: Subscription | None = None

    async def attach(self, bus: CognitiveBus) -> None:
        """Subscribe to all events on *bus*."""
        self._sub = await bus.subscribe("*", self._on_event)
        logger.debug("Trace sink attached -> %s", self._log.path)

    async def detach(self) -> None:
        """Unsubscribe from the bus (idempotent)."""
        if self._sub is not None:
            self._sub.unsubscribe()
            self._sub = None

    def _on_event(self, event: Event) -> None:
        """Serialize one event to the trace log (best-effort, never raises)."""
        try:
            data = event.data
            self._log.append(
                {
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "timestamp": event.timestamp,
                    "source": event.source,
                    "correlation_id": event.correlation_id,
                    "data": dict(data) if isinstance(data, dict) else {"value": data},
                }
            )
        except Exception as exc:  # never let tracing break the run
            logger.debug("Trace sink dropped an event: %s", exc)

    # Allow ``handler`` style attribute access if a caller wants the callable.
    @property
    def handler(self) -> Any:
        return self._on_event
