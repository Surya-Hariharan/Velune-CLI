"""The pure legacy mapper carries today's coding-council results on the neutral outcome type.

Fixtures are real ``execute_task`` results produced by the Phase 0 fake orchestrator, so the mapper
is checked against what the live pipeline actually returns, not against a guess.
"""

from __future__ import annotations

import pytest

from tests.council_fakes import FINAL, make_orchestrator
from velune.council.adapters.legacy import (
    AGENT_ERROR_KINDS,
    EXECUTION_STATUSES,
    MESSAGE_STATUSES,
    outcome_from_legacy_result,
    outcome_status_from_execution,
    seat_status_from_agent_kind,
    seat_status_from_message_status,
)
from velune.council.domain import CouncilDomain
from velune.council.results import OutcomeStatus, SeatStatus
from velune.orchestration.schemas import ExecutionStatus


async def _result(monkeypatch, responder=None, tier="standard"):
    orch, _ = make_orchestrator(monkeypatch, responder)
    return await orch.execute_task("explain it", "ctx", council_tier=tier)


async def test_healthy_result_maps_to_a_completed_outcome(monkeypatch):
    result = await _result(monkeypatch)
    outcome = outcome_from_legacy_result(result, request_id="req-1")
    assert outcome.status is OutcomeStatus.COMPLETED
    assert outcome.answer == FINAL == result["final_summary"]
    assert outcome.degradations == () and outcome.failure_summary is None
    assert outcome.profile_id == "coding" and outcome.domain is CouncilDomain.CODING
    assert outcome.request_id == "req-1"
    assert [a.kind for a in outcome.artifacts] == ["proposal"]
    assert outcome.artifacts[0].content == result["coder_proposal"]


async def test_degraded_judge_maps_to_degraded_with_reasons(monkeypatch):
    def responder(seat, request):
        if seat == "reviewer":
            return RuntimeError("provider down")
        from tests.council_fakes import healthy

        return healthy(seat, request)

    result = await _result(monkeypatch, responder)
    outcome = outcome_from_legacy_result(result)
    assert result["arbitration"]["requires_human_review"] is True  # never read as an approval
    assert outcome.answer == result["final_summary"] and outcome.answer
    assert outcome.status is (
        OutcomeStatus.DEGRADED if result["degraded"] else OutcomeStatus.COMPLETED
    )


async def test_degraded_synthesizer_maps_to_degraded(monkeypatch):
    def responder(seat, request):
        from tests.council_fakes import healthy

        if seat == "synthesizer":
            return RuntimeError("synth down")
        return healthy(seat, request)

    result = await _result(monkeypatch, responder)
    assert result["degraded"] is True
    outcome = outcome_from_legacy_result(result)
    assert outcome.status is OutcomeStatus.DEGRADED
    assert any("synthesizer_unavailable" in note for note in outcome.degradations)
    assert outcome.answer == result["final_summary"]  # the degraded report, never a sentinel
    assert "[Agent" not in outcome.answer


async def test_all_agents_failed_maps_to_failed_without_an_answer(monkeypatch):
    def responder(seat, request):
        if seat == "coder":
            return RuntimeError("down")
        from tests.council_fakes import healthy

        return healthy(seat, request)

    result = await _result(monkeypatch, responder)
    assert result["is_timeout"] is True
    outcome = outcome_from_legacy_result(result)
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.answer is None
    assert outcome.failure_summary == result["final_summary"]
    assert outcome.artifacts == ()


def test_is_timeout_without_a_summary_still_fails_with_a_reason():
    outcome = outcome_from_legacy_result({"is_timeout": True})
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.answer is None and outcome.failure_summary


def test_failure_text_never_becomes_the_answer():
    outcome = outcome_from_legacy_result({"is_timeout": True, "final_summary": "ran out of time"})
    assert outcome.answer is None
    assert outcome.failure_summary == "ran out of time"


def test_degraded_flag_without_reasons_is_still_degraded():
    outcome = outcome_from_legacy_result({"final_summary": "ok", "degraded": True})
    assert outcome.status is OutcomeStatus.DEGRADED


def test_missing_or_blank_summary_gives_no_answer_not_an_empty_string():
    assert outcome_from_legacy_result({"final_summary": "   "}).answer is None
    assert outcome_from_legacy_result({}).answer is None


def test_artifact_only_for_a_real_proposal():
    assert (
        outcome_from_legacy_result({"final_summary": "a", "coder_proposal": None}).artifacts == ()
    )
    assert outcome_from_legacy_result({"final_summary": "a", "coder_proposal": " "}).artifacts == ()


def test_long_reasons_are_clipped_to_the_contract_limit():
    outcome = outcome_from_legacy_result(
        {"final_summary": "a", "degraded": True, "degradation_reasons": ["x" * 900]}
    )
    assert len(outcome.degradations[0]) == 300


def test_the_mapper_does_not_mutate_its_input():
    result = {"final_summary": "a", "degradation_reasons": ["r"], "degraded": True}
    before = dict(result)
    outcome_from_legacy_result(result)
    assert result == before


# ── vocabulary mappings (Phase 0 <-> core) ──────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("timeout", SeatStatus.TIMEOUT),
        ("provider", SeatStatus.PROVIDER_ERROR),
        ("auth", SeatStatus.AUTH_ERROR),
        ("empty", SeatStatus.EMPTY),
        ("something-new", SeatStatus.PROVIDER_ERROR),
    ],
)
def test_council_agent_error_kinds_map_to_seat_statuses(kind, status):
    assert seat_status_from_agent_kind(kind) is status
    assert set(AGENT_ERROR_KINDS) == {"timeout", "provider", "auth", "empty"}


def test_agent_error_kinds_match_what_the_base_agent_can_raise():
    import inspect

    from velune.cognition.council import base

    source = inspect.getsource(base)
    for kind in AGENT_ERROR_KINDS:
        assert f'"{kind}"' in source


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("ok", SeatStatus.OK),
        ("unavailable", SeatStatus.PROVIDER_ERROR),
        ("unparseable", SeatStatus.UNPARSEABLE),
        ("?", SeatStatus.UNPARSEABLE),
    ],
)
def test_typed_message_statuses_map_to_seat_statuses(status, expected):
    assert seat_status_from_message_status(status) is expected
    assert set(MESSAGE_STATUSES) == {"ok", "unavailable", "unparseable"}


def test_every_message_status_the_legacy_code_defines_is_mapped():
    from typing import get_args

    from velune.cognition.council import messages

    assert set(get_args(messages.AgentStatus)) == set(MESSAGE_STATUSES)


@pytest.mark.parametrize(
    ("status", "degraded", "expected"),
    [
        (ExecutionStatus.COMPLETED, False, OutcomeStatus.COMPLETED),
        (ExecutionStatus.COMPLETED, True, OutcomeStatus.DEGRADED),
        (ExecutionStatus.FAILED, False, OutcomeStatus.FAILED),
        (ExecutionStatus.FAILED, True, OutcomeStatus.FAILED),
        (ExecutionStatus.INTERRUPTED, False, OutcomeStatus.CANCELLED),
    ],
)
def test_execution_status_maps_onto_outcome_status(status, degraded, expected):
    assert outcome_status_from_execution(status.value, degraded=degraded) is expected
    assert set(EXECUTION_STATUSES) <= {s.value for s in ExecutionStatus}
