"""The only seams between the core and the outside world.

The core knows no provider, model, key, registry or clock. It calls these ports; the runtime
adapter binds them to the existing machinery and tests bind them to fakes. ``AsyncioScheduler`` is
the reference scheduler, pure asyncio, so the core can run with no provider stack loaded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Annotated, Literal, Protocol, TypeVar, runtime_checkable

from pydantic import Field, StringConstraints

from velune.cognition.execution_trace import CallReason
from velune.council.domain import SeatKind, StageId
from velune.council.results import SeatResult
from velune.council.serialization import Contract
from velune.council.trace import CouncilTraceEvent

T = TypeVar("T")


class SeatMessage(Contract):
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, StringConstraints(min_length=1)]


class SeatCall(Contract):
    """One request to one seat. Names a seat and a reason, never a provider or model."""

    seat_id: str
    kind: SeatKind
    stage: StageId
    messages: tuple[SeatMessage, ...] = Field(min_length=1)
    reason: CallReason = CallReason.PRIMARY
    timeout_s: float = Field(default=120.0, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)


@runtime_checkable
class SeatInvoker(Protocol):
    """Runs one seat call. Never raises: every failure comes back as a typed ``SeatResult``.

    The one exception is ``asyncio.CancelledError``, which must propagate so a cancelled run
    actually stops.
    """

    async def invoke(self, call: SeatCall) -> SeatResult[str]: ...


@runtime_checkable
class TraceSink(Protocol):
    def emit(self, event: CouncilTraceEvent) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def monotonic(self) -> float: ...


@runtime_checkable
class IdSource(Protocol):
    def next_id(self, prefix: str) -> str: ...


@runtime_checkable
class PromptSource(Protocol):
    """Static prompt text for a seat. The core only assembles it; it never holds the wording."""

    def shared_prompt(self) -> str: ...

    def role_prompt(self, seat_id: str, stage: StageId) -> str: ...


@runtime_checkable
class AssignmentSource(Protocol):
    """Names which authors a viewer is assigned to read (the review graph, a later phase)."""

    def assigned_authors(self, stage: StageId, viewer: str) -> tuple[str, ...]: ...


@runtime_checkable
class Scheduler(Protocol):
    async def run(
        self,
        jobs: Sequence[Callable[[], Awaitable[T]]],
        *,
        max_concurrency: int = 1,
    ) -> list[T | BaseException]:
        """Run jobs, returning one entry per job in input order.

        A job that raises yields its exception in place; it never disturbs its siblings.
        ``CancelledError`` is not isolated: it cancels every pending job and propagates.
        """
        ...


class AsyncioScheduler:
    """Order-preserving, failure-isolating scheduler over plain asyncio."""

    async def run(
        self,
        jobs: Sequence[Callable[[], Awaitable[T]]],
        *,
        max_concurrency: int = 1,
    ) -> list[T | BaseException]:
        gate = asyncio.Semaphore(max(1, max_concurrency))

        async def guarded(job: Callable[[], Awaitable[T]]) -> T | BaseException:
            async with gate:
                try:
                    return await job()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    return exc

        tasks = [asyncio.ensure_future(guarded(job)) for job in jobs]
        try:
            return list(await asyncio.gather(*tasks))
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
