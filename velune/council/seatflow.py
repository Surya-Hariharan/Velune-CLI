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

from collections.abc import Callable
from typing import Any, TypeVar

from velune.cognition.execution_trace import CallReason
from velune.council.parsing import DraftParseError, parse_and_convert, repair_messages
from velune.council.ports import SeatCall, SeatInvoker
from velune.council.results import SeatResult, SeatStatus
from velune.council.serialization import Contract
from velune.council.trace import TraceEventKind

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


def note_fallback(result: SeatResult[Any], emit: Emit) -> None:
    """Record that a different model answered. This is information, not degradation."""
    if result.fallback_used and result.model is not None:
        emit(
            TraceEventKind.SEAT_FALLBACK,
            seat=result.seat_id,
            detail=f"{result.model.provider_id}/{result.model.model_id}",
        )
