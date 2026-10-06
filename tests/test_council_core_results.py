"""Failure invariants and deterministic status derivation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from velune.council.contracts import Artifact
from velune.council.domain import (
    CouncilDomain,
    Criticality,
    QuorumRule,
    SeatKind,
    StageId,
)
from velune.council.results import (
    CouncilOutcome,
    OutcomeStatus,
    SeatError,
    SeatResult,
    SeatStatus,
    StageResult,
    StageStatus,
    derive_outcome_status,
    derive_stage_status,
    quorum_met,
)
from velune.council.serialization import canonical_json, digest, full_json

P = StageId.PERSPECTIVES


def seat(seat_id: str, status: SeatStatus = SeatStatus.OK, **kw) -> SeatResult:
    if status is SeatStatus.OK:
        return SeatResult(
            seat_id=seat_id,
            kind=SeatKind.PERSPECTIVE,
            stage=P,
            status=status,
            payload=kw.pop("payload", "text"),
            **kw,
        )
    return SeatResult.failure(
        seat_id=seat_id, kind=SeatKind.PERSPECTIVE, stage=P, status=status, **kw
    )


def stage(status: StageStatus, which: StageId = P, **kw) -> StageResult:
    if status is StageStatus.SKIPPED:
        kw.setdefault("skipped_reason", "upstream_failed:frame")
    return StageResult(stage=which, status=status, **kw)


def outcome(status: OutcomeStatus, **kw) -> CouncilOutcome:
    if status is OutcomeStatus.FAILED:
        kw.setdefault("failure_summary", "a required stage failed")
    return CouncilOutcome(
        request_id="r",
        profile_id="general",
        domain=CouncilDomain.GENERAL,
        status=status,
        **kw,
    )


NON_OK = [s for s in SeatStatus if s is not SeatStatus.OK]


@pytest.mark.parametrize("status", NON_OK)
def test_a_seat_that_did_not_deliver_cannot_carry_a_payload(status):
    with pytest.raises(ValidationError):
        SeatResult(
            seat_id="analyst",
            kind=SeatKind.PERSPECTIVE,
            stage=P,
            status=status,
            payload="looks fine",
        )


@pytest.mark.parametrize("status", NON_OK)
def test_failure_constructor_yields_a_payloadless_typed_result(status):
    result = seat("analyst", status, message="boom")
    assert result.payload is None and not result.ok
    assert result.error is not None and result.error.kind is status


def test_failure_constructor_refuses_ok_and_truncates_messages():
    with pytest.raises(ValueError):
        SeatResult.failure(
            seat_id="analyst", kind=SeatKind.PERSPECTIVE, stage=P, status=SeatStatus.OK
        )
    assert len(seat("analyst", SeatStatus.TIMEOUT, message="x" * 900).error.message) == 300


def test_an_ok_seat_needs_a_payload_and_no_error():
    with pytest.raises(ValidationError):
        SeatResult(seat_id="analyst", kind=SeatKind.PERSPECTIVE, stage=P, status=SeatStatus.OK)
    with pytest.raises(ValidationError):
        SeatResult(
            seat_id="analyst",
            kind=SeatKind.PERSPECTIVE,
            stage=P,
            status=SeatStatus.OK,
            payload="x",
            error=SeatError(kind=SeatStatus.TIMEOUT),
        )


def test_timing_is_volatile_and_model_identity_is_not():
    fast, slow = seat("analyst", elapsed_ms=1), seat("analyst", elapsed_ms=900)
    assert digest(fast) == digest(slow)
    assert '"elapsed_ms":900' in full_json(slow)
    assert "elapsed_ms" not in canonical_json(slow)


@pytest.mark.parametrize("status", [OutcomeStatus.FAILED, OutcomeStatus.CANCELLED])
def test_failed_or_cancelled_outcomes_never_carry_an_answer(status):
    with pytest.raises(ValidationError):
        outcome(status, answer="It worked!", failure_summary="x")
    assert outcome(status).answer is None


def test_failed_outcome_must_say_what_failed_and_only_failures_may_say_so():
    with pytest.raises(ValidationError):
        CouncilOutcome(
            request_id="r",
            profile_id="general",
            domain=CouncilDomain.GENERAL,
            status=OutcomeStatus.FAILED,
        )
    with pytest.raises(ValidationError):
        outcome(OutcomeStatus.COMPLETED, failure_summary="nothing wrong")


def test_successful_outcomes_may_carry_an_answer_and_inert_artifacts():
    done = outcome(
        OutcomeStatus.DEGRADED, answer="Final", artifacts=(Artifact(kind="text", content="c"),)
    )
    assert done.succeeded and done.answer == "Final"
    assert not outcome(OutcomeStatus.FAILED).succeeded


def test_skipped_stage_must_have_a_reason_and_nothing_else_may():
    with pytest.raises(ValidationError):
        StageResult(stage=P, status=StageStatus.SKIPPED)
    with pytest.raises(ValidationError):
        StageResult(stage=P, status=StageStatus.COMPLETED, skipped_reason="why")


# ── quorum ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rule", "ok", "expected"),
    [
        (QuorumRule(min_ok=0), [], True),
        (QuorumRule(min_ok=1), [], False),
        (QuorumRule(min_ok=2), ["a", "b"], True),
        (QuorumRule(min_ok=2), ["a"], False),
        (QuorumRule(min_ok=2), ["a", "a"], False),  # the same seat twice is one seat
        (QuorumRule(min_ok=1, require_any_of=("x", "y")), ["a"], False),
        (QuorumRule(min_ok=1, require_any_of=("x", "y")), ["a", "y"], True),
        (QuorumRule(min_ok=3, require_any_of=("x",)), ["a", "b", "x"], True),
        (QuorumRule(min_ok=3, require_any_of=("x",)), ["a", "b", "c"], False),
    ],
)
def test_quorum_met(rule, ok, expected):
    assert quorum_met(rule, ok) is expected


# ── stage status derivation (table driven) ──────────────────────────────────

SEATS = ("sa", "sb", "sc", "sd", "se")
QUORUM = QuorumRule(min_ok=3, require_any_of=("sb", "sd"))


def results(**statuses: SeatStatus) -> list[SeatResult]:
    return [seat(name, statuses.get(name, SeatStatus.OK)) for name in SEATS]


@pytest.mark.parametrize(
    ("statuses", "criticality", "expected", "notes"),
    [
        ({}, Criticality.REQUIRED, StageStatus.COMPLETED, ()),
        (
            {"sa": SeatStatus.TIMEOUT},
            Criticality.REQUIRED,
            StageStatus.DEGRADED,
            ("seat_unavailable:sa",),
        ),
        (
            {"sa": SeatStatus.TIMEOUT, "sc": SeatStatus.EMPTY},
            Criticality.REQUIRED,
            StageStatus.DEGRADED,
            ("seat_unavailable:sa", "seat_unavailable:sc"),
        ),
        (
            {"sa": SeatStatus.TIMEOUT, "sc": SeatStatus.EMPTY, "se": SeatStatus.BLOCKED},
            Criticality.REQUIRED,
            StageStatus.FAILED,
            ("seat_unavailable:sa", "seat_unavailable:sc", "seat_unavailable:se", "quorum_not_met"),
        ),
        (
            {"sb": SeatStatus.PROVIDER_ERROR, "sd": SeatStatus.UNPARSEABLE},
            Criticality.REQUIRED,
            StageStatus.FAILED,  # 3 delivered but none from the required set
            ("seat_unavailable:sb", "seat_unavailable:sd", "quorum_not_met"),
        ),
        (
            {"sb": SeatStatus.AUTH_ERROR, "sc": SeatStatus.CANCELLED, "se": SeatStatus.SKIPPED},
            Criticality.OPTIONAL,
            StageStatus.DEGRADED,  # an optional stage never fails the run
            ("seat_unavailable:sb", "seat_unavailable:sc", "seat_unavailable:se", "quorum_not_met"),
        ),
    ],
)
def test_stage_status_with_a_quorum(statuses, criticality, expected, notes):
    status, got = derive_stage_status(
        expected_seats=SEATS,
        seat_results=results(**statuses),
        quorum=QUORUM,
        criticality=criticality,
    )
    assert (status, got) == (expected, notes)


def test_without_a_quorum_every_expected_seat_is_required():
    base = {"expected_seats": ("sa", "sb"), "quorum": None, "criticality": Criticality.REQUIRED}
    ok = derive_stage_status(seat_results=[seat("sa"), seat("sb")], **base)
    assert ok == (StageStatus.COMPLETED, ())
    missing = derive_stage_status(seat_results=[seat("sa")], **base)
    assert missing[0] is StageStatus.FAILED and "seat_unavailable:sb" in missing[1]


def test_a_stage_with_no_seats_is_trivially_complete():
    assert derive_stage_status(
        expected_seats=(), seat_results=[], quorum=None, criticality=Criticality.REQUIRED
    ) == (StageStatus.COMPLETED, ())


def test_absent_seats_are_recorded_not_defaulted():
    status, notes = derive_stage_status(
        expected_seats=SEATS,
        seat_results=results(sa=SeatStatus.TIMEOUT),
        quorum=QUORUM,
        criticality=Criticality.REQUIRED,
    )
    assert status is StageStatus.DEGRADED and notes == ("seat_unavailable:sa",)


# ── outcome status derivation (table driven) ────────────────────────────────


@pytest.mark.parametrize(
    ("stages", "kwargs", "expected"),
    [
        ([], {}, OutcomeStatus.COMPLETED),
        ([StageStatus.COMPLETED, StageStatus.COMPLETED], {}, OutcomeStatus.COMPLETED),
        ([StageStatus.COMPLETED, StageStatus.DEGRADED], {}, OutcomeStatus.DEGRADED),
        ([StageStatus.COMPLETED], {"deterministic_fallback_used": True}, OutcomeStatus.DEGRADED),
        ([StageStatus.DEGRADED, StageStatus.FAILED], {}, OutcomeStatus.FAILED),
        ([StageStatus.FAILED, StageStatus.SKIPPED], {}, OutcomeStatus.FAILED),
        ([StageStatus.COMPLETED], {"cancelled": True}, OutcomeStatus.CANCELLED),
        ([StageStatus.FAILED], {"cancelled": True}, OutcomeStatus.CANCELLED),
    ],
)
def test_outcome_status(stages, kwargs, expected):
    results_ = [stage(s) for s in stages]
    assert derive_outcome_status(results_, **kwargs) is expected


def test_a_model_fallback_that_succeeded_is_not_degradation():
    fell_back = seat("sa", fallback_used=True)
    done = StageResult(stage=P, status=StageStatus.COMPLETED, seat_results=(fell_back,))
    assert derive_outcome_status([done]) is OutcomeStatus.COMPLETED
    assert done.seat_results[0].fallback_used is True
