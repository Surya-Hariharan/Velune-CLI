"""One seat call, parsed into its contract, with at most one repair.

Both deliberative stages use this single flow, so the failure semantics cannot diverge between
them:

* a seat that did not deliver comes back as its typed failure, unchanged, with no payload;
* a delivered reply that does not parse earns exactly one repair request to the same seat, carrying
  only the seat's own reply and the field errors (``CallReason.VALIDATION_FAILURE``);
* a second failure is an absence (``unparseable``), never a guess or a default.

Provider retry and model fallback are not handled here: they live behind the ``SeatInvoker`` port.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, TypeVar

from velune.cognition.execution_trace import CallReason
from velune.council.parsing import DraftParseError, parse_and_convert, repair_messages
from velune.council.ports import SeatCall, SeatInvoker
from velune.council.results import SeatResult, SeatStatus
from velune.council.serialization import Contract
from velune.council.trace import TraceEventKind

if TYPE_CHECKING:
    from velune.council.profiles import SeatSpec
    from velune.council.state import StageContext

D = TypeVar("D", bound=Contract)

Emit = Callable[..., None]


def _paths(errors: tuple[str, ...]) -> str:
    return "; ".join(error.split(":", 1)[0] for error in errors[:3])


def _delivered(
    first: SeatResult[str], value: Any, *, attempts: int, elapsed_ms: int
) -> SeatResult[Any]:
    return SeatResult(
        seat_id=first.seat_id,
        kind=first.kind,
        stage=first.stage,
        status=SeatStatus.OK,
        payload=value,
        model=first.model,
        attempts=attempts,
        fallback_used=first.fallback_used,
        elapsed_ms=elapsed_ms,
    )


async def call_and_parse(
    *,
    invoker: SeatInvoker,
    call: SeatCall,
    draft_model: type[D],
    convert: Callable[[D], Any],
    emit: Emit,
) -> SeatResult[Any]:
    """Invoke ``call`` and return a ``SeatResult`` whose payload is the converted contract."""
    first = await invoker.invoke(call)
    if not first.ok:
        return first
    assert first.payload is not None
    try:
        value = parse_and_convert(first.payload, draft_model, convert)
    except DraftParseError as exc:
        errors = exc.errors
    else:
        return _delivered(first, value, attempts=first.attempts, elapsed_ms=first.elapsed_ms)

    emit(TraceEventKind.SEAT_REPAIR, seat=call.seat_id, detail=_paths(errors))
    repair = call.model_copy(
        update={
            "messages": repair_messages(call.messages, first.payload, errors),
            "reason": CallReason.VALIDATION_FAILURE,
        }
    )
    second = await invoker.invoke(repair)
    attempts = first.attempts + second.attempts
    elapsed = first.elapsed_ms + second.elapsed_ms
    used_fallback = first.fallback_used or second.fallback_used
    if not second.ok:
        return SeatResult.failure(
            seat_id=call.seat_id,
            kind=call.kind,
            stage=call.stage,
            status=second.status,
            message=second.error.message if second.error else "",
            model=second.model or first.model,
            attempts=attempts,
            fallback_used=used_fallback,
            elapsed_ms=elapsed,
        )
    assert second.payload is not None
    try:
        value = parse_and_convert(second.payload, draft_model, convert)
    except DraftParseError as exc:
        return SeatResult.failure(
            seat_id=call.seat_id,
            kind=call.kind,
            stage=call.stage,
            status=SeatStatus.UNPARSEABLE,
            message="; ".join(exc.errors)[:300],
            model=second.model or first.model,
            attempts=attempts,
            fallback_used=used_fallback,
            elapsed_ms=elapsed,
        )
    return SeatResult(
        seat_id=call.seat_id,
        kind=call.kind,
        stage=call.stage,
        status=SeatStatus.OK,
        payload=value,
        model=second.model or first.model,
        attempts=attempts,
        fallback_used=used_fallback,
        elapsed_ms=elapsed,
    )


DEADLINE_MARGIN_S = 5.0
DEADLINE_MARGIN_SHARE = 0.10
STAGE_DEADLINE = "stage_deadline"


def deadline_for(allowance_s: float) -> float:
    """The helper's own deadline: the stage allowance less a margin, so it fires before the runner's."""
    return allowance_s - min(DEADLINE_MARGIN_S, allowance_s * DEADLINE_MARGIN_SHARE)


async def run_seat_jobs(
    ctx: StageContext,
    seats: Sequence[SeatSpec],
    make_job: Callable[[SeatSpec], Callable[[], Awaitable[SeatResult[Any]]]],
    *,
    on_error: Callable[[SeatSpec, Exception], None] | None = None,
) -> tuple[list[SeatResult[Any]], tuple[str, ...]]:
    """Run one job per seat through the scheduler, keeping what finished if the stage runs out of time.

    Each job records its result the moment it completes, so when the internal deadline (just before
    the runner's stage timeout) fires, every seat that already delivered is kept and every other
    seat becomes a ``timeout`` absence with no payload. A job that raises becomes a typed
    ``provider_error`` at once, so it is never relabelled ``timeout`` because a sibling ran late.
    Cancellation is not a timeout: ``CancelledError`` propagates and the scheduler cancels the rest.

    Returns the results in seat order and the degradation notes (``stage_deadline`` if it fired).
    """
    done: dict[str, SeatResult[Any]] = {}

    def wrap(seat: SeatSpec) -> Callable[[], Awaitable[SeatResult[Any]]]:
        job = make_job(seat)

        async def run() -> SeatResult[Any]:
            try:
                result = await job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if on_error is not None:
                    on_error(seat, exc)
                result = SeatResult.failure(
                    seat_id=seat.id,
                    kind=seat.kind,
                    stage=ctx.contract.stage,
                    status=SeatStatus.PROVIDER_ERROR,
                    message=f"internal_error:{type(exc).__name__}",
                )
            done[seat.id] = result
            return result

        return run

    notes: tuple[str, ...] = ()
    try:
        await asyncio.wait_for(
            ctx.scheduler.run(
                [wrap(seat) for seat in seats], max_concurrency=ctx.settings.max_concurrency
            ),
            timeout=deadline_for(ctx.timeout_s),
        )
    except asyncio.TimeoutError:
        notes = (STAGE_DEADLINE,)
    results: list[SeatResult[Any]] = []
    for seat in seats:
        found = done.get(seat.id)
        results.append(
            found
            if found is not None
            else SeatResult.failure(
                seat_id=seat.id,
                kind=seat.kind,
                stage=ctx.contract.stage,
                status=SeatStatus.TIMEOUT,
                message="the stage deadline passed before this seat finished",
            )
        )
    return results, notes


class SeatEvents:
    """Trace events raised inside parallel seat jobs, flushed afterwards in seat order.

    Jobs run in whatever order the scheduler picks; flushing in seat order makes the trace the same
    at any concurrency.
    """

    def __init__(self, seats: Sequence[SeatSpec]) -> None:
        self._events: dict[str, list[tuple[TraceEventKind, dict[str, Any]]]] = {
            seat.id: [] for seat in seats
        }

    def emitter(self, seat: SeatSpec) -> Emit:
        def emit(kind: TraceEventKind, **fields: Any) -> None:
            fields.pop("seat", None)  # the flush attributes events to the seat
            self._events[seat.id].append((kind, fields))

        return emit

    def record_error(self, seat: SeatSpec, exc: Exception) -> None:
        self._events[seat.id].append((TraceEventKind.SEAT_ERROR, {"detail": type(exc).__name__}))

    def flush(self, ctx: StageContext, seats: Sequence[SeatSpec]) -> None:
        for seat in seats:
            for kind, fields in self._events[seat.id]:
                ctx.emit(kind, seat=seat.id, **fields)


def refuse(result: SeatResult[Any], why: str) -> SeatResult[Any]:
    """Turn a delivered result the content screen refused into a ``blocked`` absence.

    The payload is dropped (a seat that did not deliver carries none); the seat's model identity,
    attempts and timing are kept so the record still says what ran.
    """
    return SeatResult.failure(
        seat_id=result.seat_id,
        kind=result.kind,
        stage=result.stage,
        status=SeatStatus.BLOCKED,
        message=why,
        model=result.model,
        attempts=result.attempts,
        fallback_used=result.fallback_used,
        elapsed_ms=result.elapsed_ms,
    )


def note_fallback(result: SeatResult[Any], emit: Emit) -> None:
    """Record that a different model answered. This is information, not degradation."""
    if result.fallback_used and result.model is not None:
        emit(
            TraceEventKind.SEAT_FALLBACK,
            seat=result.seat_id,
            detail=f"{result.model.provider_id}/{result.model.model_id}",
        )
