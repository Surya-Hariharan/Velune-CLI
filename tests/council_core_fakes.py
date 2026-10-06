"""Fakes for the council core: scripted seat invoker, fixed clock, counter ids, assignments.

No provider, network, terminal or wall clock is involved, so every test using these is exact.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Callable

from velune.council.domain import SeatKind, StageId
from velune.council.ports import SeatCall
from velune.council.results import SeatResult, SeatStatus

Script = SeatResult | str | BaseException | Callable[[SeatCall], object]


class FakeSeatInvoker:
    """Answers each ``(seat_id, stage)`` from a script and records every call it receives.

    A script entry may be text (an ok result), a ``SeatStatus`` (a typed failure), a ready
    ``SeatResult``, an exception to raise (to prove callers cope), or a callable taking the call.
    Entries are consumed in order; the last one repeats.
    """

    def __init__(self, scripts: dict[tuple[str, StageId], list[Script]] | None = None) -> None:
        self.scripts: dict[tuple[str, StageId], list[Script]] = defaultdict(list)
        for key, value in (scripts or {}).items():
            self.scripts[key] = list(value)
        self.calls: list[SeatCall] = []
        self.delay_s: float = 0.0
        self.in_flight = 0
        self.max_in_flight = 0

    def script(self, seat_id: str, stage: StageId, *entries: Script) -> None:
        self.scripts[(seat_id, stage)] = list(entries)

    async def invoke(self, call: SeatCall) -> SeatResult[str]:
        self.calls.append(call)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            queue = self.scripts.get((call.seat_id, call.stage))
            entry: Script = f"{call.seat_id}:{call.stage.value}" if not queue else queue[0]
            if queue and len(queue) > 1:
                queue.pop(0)
            return self._resolve(call, entry)
        finally:
            self.in_flight -= 1

    def _resolve(self, call: SeatCall, entry: Script) -> SeatResult[str]:
        if callable(entry) and not isinstance(entry, (SeatResult, str, BaseException)):
            entry = entry(call)  # type: ignore[assignment]
        if isinstance(entry, BaseException):
            raise entry
        if isinstance(entry, SeatResult):
            return entry
        if isinstance(entry, SeatStatus):
            return SeatResult.failure(
                seat_id=call.seat_id, kind=call.kind, stage=call.stage, status=entry
            )
        return SeatResult(
            seat_id=call.seat_id,
            kind=call.kind,
            stage=call.stage,
            status=SeatStatus.OK,
            payload=str(entry),
        )

    def calls_for(self, seat_id: str, stage: StageId | None = None) -> list[SeatCall]:
        return [
            c for c in self.calls if c.seat_id == seat_id and (stage is None or c.stage is stage)
        ]


class FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class CounterIds:
    def __init__(self) -> None:
        self.counts: dict[str, int] = defaultdict(int)

    def next_id(self, prefix: str) -> str:
        self.counts[prefix] += 1
        return f"{prefix}-{self.counts[prefix]}"


class FakeAssignments:
    """Review-graph stand-in: ``{(stage, viewer): (author, ...)}``."""

    def __init__(self, mapping: dict[tuple[StageId, str], tuple[str, ...]] | None = None) -> None:
        self.mapping = mapping or {}

    def assigned_authors(self, stage: StageId, viewer: str) -> tuple[str, ...]:
        return self.mapping.get((stage, viewer), ())


def ok_result(seat_id: str, stage: StageId, text: str = "x", kind=SeatKind.PERSPECTIVE):
    return SeatResult(seat_id=seat_id, kind=kind, stage=stage, status=SeatStatus.OK, payload=text)
