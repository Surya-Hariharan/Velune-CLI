"""Deterministic fakes for the orchestration engine: a scripted reasoning invoker and a trace log.

No provider, network, terminal or wall clock is involved. The clock and id fakes are the council
core's own (``tests.council_core_fakes``), reused unchanged.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable

from velune.council.results import SeatStatus
from velune.council.trace import CouncilTraceEvent
from velune.orchestration.engine.ports import ReasoningRequest, ReasoningResult

Script = ReasoningResult | str | SeatStatus | BaseException | Callable[[ReasoningRequest], object]


class FakeReasoningInvoker:
    """Answers each request from a script and records every request it receives.

    An entry may be text (an ok result), a ``SeatStatus`` (a typed failure), a ready
    ``ReasoningResult``, an exception to raise (to prove the runner copes) or a callable taking the
    request. Entries are consumed in order and the last one repeats.
    """

    def __init__(self, *script: Script, delay_s: float = 0.0) -> None:
        self._script: deque[Script] = deque(script or ("ok",))
        self.requests: list[ReasoningRequest] = []
        self.delay_s = delay_s

    async def invoke(self, request: ReasoningRequest) -> ReasoningResult:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        entry = self._script[0]
        if len(self._script) > 1:
            self._script.popleft()
        if callable(entry) and not isinstance(entry, (ReasoningResult, str, BaseException)):
            entry = entry(request)  # type: ignore[assignment]
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, ReasoningResult):
            return entry
        if isinstance(entry, SeatStatus):
            return ReasoningResult.failure(entry, f"scripted {entry.value}")
        return ReasoningResult.success(str(entry))


class TraceLog:
    def __init__(self) -> None:
        self.events: list[CouncilTraceEvent] = []

    def emit(self, event: CouncilTraceEvent) -> None:
        self.events.append(event)


class ExplodingTrace:
    def emit(self, event: CouncilTraceEvent) -> None:
        raise RuntimeError("sink down")
