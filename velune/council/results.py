"""Seat, stage and run results, and the deterministic derivation of their statuses.

The invariant that matters most lives here: **a failure never looks like success.** A seat that did
not deliver carries no payload, a failed or cancelled run carries no answer, and an absent opinion is
recorded as absent rather than defaulted to agreement.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, ClassVar, Generic, Literal, TypeVar

from pydantic import Field, StringConstraints, model_validator

from velune._compat import StrEnum
from velune.council.contracts import Artifact, Slug
from velune.council.domain import (
    ArtifactKind,
    CouncilDomain,
    Criticality,
    QuorumRule,
    SeatKind,
    StageId,
)
from velune.council.serialization import SCHEMA_VERSION, Contract

T = TypeVar("T")

ShortText = Annotated[str, StringConstraints(max_length=300)]


class SeatStatus(StrEnum):
    OK = "ok"
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    AUTH_ERROR = "auth_error"
    EMPTY = "empty"
    UNPARSEABLE = "unparseable"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class SeatError(Contract):
    """Why a seat did not deliver. Structural detail only; never prompt or completion text."""

    kind: SeatStatus
    message: ShortText = ""


class ModelRef(Contract):
    provider_id: str = Field(max_length=64)
    model_id: str = Field(max_length=128)


class SeatResult(Contract, Generic[T]):
    seat_id: Slug
    kind: SeatKind
    stage: StageId
    status: SeatStatus
    payload: T | None = None
    error: SeatError | None = None
    model: ModelRef | None = None
    attempts: int = Field(default=1, ge=0)
    fallback_used: bool = False
    elapsed_ms: int = Field(default=0, ge=0)

    volatile_fields: ClassVar[frozenset[str]] = frozenset({"elapsed_ms"})

    @model_validator(mode="after")
    def _failure_is_not_success(self) -> SeatResult[T]:
        if self.status is SeatStatus.OK:
            if self.payload is None:
                raise ValueError("an ok seat result must carry a payload")
            if self.error is not None:
                raise ValueError("an ok seat result cannot carry an error")
        else:
            if self.payload is not None:
                raise ValueError("a seat that did not deliver cannot carry a payload")
            if self.error is not None and self.error.kind is SeatStatus.OK:
                raise ValueError("an error cannot have status ok")
        return self

    @property
    def ok(self) -> bool:
        return self.status is SeatStatus.OK

    @classmethod
    def failure(
        cls,
        *,
        seat_id: str,
        kind: SeatKind,
        stage: StageId,
        status: SeatStatus,
        message: str = "",
        model: ModelRef | None = None,
        attempts: int = 1,
        fallback_used: bool = False,
        elapsed_ms: int = 0,
    ) -> SeatResult[Any]:
        if status is SeatStatus.OK:
            raise ValueError("failure() needs a non-ok status")
        return cls(
            seat_id=seat_id,
            kind=kind,
            stage=stage,
            status=status,
            error=SeatError(kind=status, message=message[:300]),
            model=model,
            attempts=attempts,
            fallback_used=fallback_used,
            elapsed_ms=elapsed_ms,
        )


class StageStatus(StrEnum):
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ArtifactRef(Contract):
    """A pointer to an artifact a stage wrote, without carrying its content."""

    kind: ArtifactKind
    author: Slug
    target: Slug | None = None


class StageResult(Contract):
    stage: StageId
    status: StageStatus
    seat_results: tuple[SeatResult[Any], ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    degradations: tuple[ShortText, ...] = ()
    skipped_reason: ShortText | None = None

    @model_validator(mode="after")
    def _skipped_has_a_reason(self) -> StageResult:
        if self.status is StageStatus.SKIPPED and not self.skipped_reason:
            raise ValueError("a skipped stage must say why")
        if self.status is not StageStatus.SKIPPED and self.skipped_reason:
            raise ValueError("only a skipped stage carries a skipped_reason")
        return self


class OutcomeStatus(StrEnum):
    COMPLETED = "completed"
    DEGRADED = "degraded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CouncilOutcome(Contract):
    schema_version: Literal[1] = SCHEMA_VERSION
    request_id: str
    profile_id: str
    domain: CouncilDomain
    status: OutcomeStatus
    answer: str | None = None
    artifacts: tuple[Artifact, ...] = ()
    stage_results: tuple[StageResult, ...] = ()
    degradations: tuple[ShortText, ...] = ()
    failure_summary: ShortText | None = None
    trace_digest: str = ""

    @model_validator(mode="after")
    def _failure_has_no_answer(self) -> CouncilOutcome:
        failed = self.status in (OutcomeStatus.FAILED, OutcomeStatus.CANCELLED)
        if failed and self.answer is not None:
            raise ValueError("a failed or cancelled run cannot carry an answer")
        if self.status is OutcomeStatus.FAILED and not self.failure_summary:
            raise ValueError("a failed run must carry a failure_summary")
        if not failed and self.failure_summary is not None:
            raise ValueError("failure_summary belongs to failed or cancelled runs only")
        return self

    @property
    def succeeded(self) -> bool:
        return self.status in (OutcomeStatus.COMPLETED, OutcomeStatus.DEGRADED)


def quorum_met(quorum: QuorumRule, ok_seat_ids: Sequence[str]) -> bool:
    ok = set(ok_seat_ids)
    if len(ok) < quorum.min_ok:
        return False
    return not quorum.require_any_of or bool(ok & set(quorum.require_any_of))


def derive_stage_status(
    *,
    expected_seats: Sequence[str],
    seat_results: Sequence[SeatResult[Any]],
    quorum: QuorumRule | None,
    criticality: Criticality,
) -> tuple[StageStatus, tuple[str, ...]]:
    """Pure status for a stage that ran, plus the degradation notes that explain it.

    Without a quorum every expected seat is required. Missing seats are always recorded; they
    degrade a stage whose quorum still holds and fail one whose quorum does not (an optional stage
    degrades instead, so a run is never failed by something it declared optional).
    """
    ok_ids = tuple(r.seat_id for r in seat_results if r.ok)
    missing = tuple(seat for seat in expected_seats if seat not in ok_ids)
    notes = tuple(f"seat_unavailable:{seat}" for seat in missing)
    rule = quorum if quorum is not None else QuorumRule(min_ok=len(expected_seats))
    if not quorum_met(rule, ok_ids):
        notes += ("quorum_not_met",)
        failed = criticality is Criticality.REQUIRED
        return (StageStatus.FAILED if failed else StageStatus.DEGRADED), notes
    return (StageStatus.DEGRADED if missing else StageStatus.COMPLETED), notes


def derive_outcome_status(
    stage_results: Sequence[StageResult],
    *,
    cancelled: bool = False,
    deterministic_fallback_used: bool = False,
) -> OutcomeStatus:
    """Cancelled > failed > degraded > completed. A model fallback that worked is not degradation."""
    if cancelled:
        return OutcomeStatus.CANCELLED
    statuses = {result.status for result in stage_results}
    if StageStatus.FAILED in statuses:
        return OutcomeStatus.FAILED
    if StageStatus.DEGRADED in statuses or deterministic_fallback_used:
        return OutcomeStatus.DEGRADED
    return OutcomeStatus.COMPLETED
