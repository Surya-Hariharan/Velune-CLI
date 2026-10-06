"""An ordered, timestamp-free record of what the runner did.

Events carry a sequence number instead of a time so the same run always produces the same trace and
the same digest. Real timings live on the volatile fields of seat results, not here.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import StringConstraints

from velune._compat import StrEnum
from velune.council.domain import StageId
from velune.council.serialization import Contract, digest


class TraceEventKind(StrEnum):
    RUN_STARTED = "run_started"
    STAGE_STARTED = "stage_started"
    SEAT_RESULT = "seat_result"
    STAGE_FINISHED = "stage_finished"
    STAGE_SKIPPED = "stage_skipped"
    STAGE_ERROR = "stage_error"
    STAGE_TIMEOUT = "stage_timeout"
    CONTRACT_VIOLATION = "contract_violation"
    CANCELLED = "cancelled"
    RUN_FINISHED = "run_finished"
    # Deliberative stages (R0/R1). Details carry field paths or type names only, never text.
    SEAT_REPAIR = "seat_repair"
    SEAT_FALLBACK = "seat_fallback"
    FRAME_FALLBACK = "frame_fallback"
    SEAT_ERROR = "seat_error"


class CouncilTraceEvent(Contract):
    seq: int
    event: TraceEventKind
    stage: StageId | None = None
    seat: str | None = None
    status: str = ""
    detail: Annotated[str, StringConstraints(max_length=300)] = ""


class TraceLog:
    """Collects events in order and forwards them to an optional sink."""

    def __init__(self, sink: Any = None) -> None:
        self._events: list[CouncilTraceEvent] = []
        self._sink = sink

    def emit(
        self,
        event: TraceEventKind,
        *,
        stage: StageId | None = None,
        seat: str | None = None,
        status: str = "",
        detail: str = "",
    ) -> CouncilTraceEvent:
        item = CouncilTraceEvent(
            seq=len(self._events),
            event=event,
            stage=stage,
            seat=seat,
            status=status,
            detail=detail[:300],
        )
        self._events.append(item)
        if self._sink is not None:
            try:
                self._sink.emit(item)
            except Exception:  # a broken sink must never break a run
                self._sink = None
        return item

    @property
    def events(self) -> tuple[CouncilTraceEvent, ...]:
        return tuple(self._events)

    def digest(self) -> str:
        return digest(self.events)
