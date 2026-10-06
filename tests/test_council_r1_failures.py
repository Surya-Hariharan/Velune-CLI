"""Failure semantics for R0/R1: failures are absences, never approvals; no failed run has an answer."""

from __future__ import annotations

import asyncio
import itertools

import pytest

from tests.council_core_fakes import FakeSeatInvoker
from tests.council_fakes import Delay, FakeMapper, FakeProvider, make_model
from tests.council_scripted import (
    PERSPECTIVE_SEATS,
    make_request,
    sentinel,
    valid_frame_json,
    valid_perspective_json,
)
from tests.council_wire import ScriptedProvider, explore, runtime_invoker
from tests.test_council_runtime_adapter import ChainMapper
from velune.core.errors.provider import ProviderAuthenticationError, ProviderConnectionError
from velune.council.domain import StageId
from velune.council.report import frame_of, perspectives_of
from velune.council.request import CouncilSettings
from velune.council.results import OutcomeStatus, SeatStatus, StageStatus
from velune.council.runner import CouncilCancelled
from velune.council.serialization import canonical_json
from velune.council.trace import TraceEventKind

P = StageId.PERSPECTIVES
F = StageId.FRAME
NON_OK = [s for s in SeatStatus if s is not SeatStatus.OK]
REQUIRED_ANY = {"skeptic", "fact_checker"}


class Sink:
    def __init__(self):
        self.items = []

    def emit(self, event):
        self.items.append(event)

    def kinds(self):
        return [e.event for e in self.items]


def run(coro, timeout: float = 30):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def fake(failing: dict[str, object] | None = None, moderator=None) -> FakeSeatInvoker:
    invoker = FakeSeatInvoker()
    invoker.script(
        "moderator", F, *([moderator] if moderator is not None else [valid_frame_json()])
    )
    for seat in PERSPECTIVE_SEATS:
        entry = (failing or {}).get(seat, valid_perspective_json(seat))
        invoker.script(seat, P, *(entry if isinstance(entry, list) else [entry]))
    return invoker


def explore_fake(invoker, request=None, sink=None):
    return run(explore(invoker, request or make_request(), sink=sink))


def stage(outcome, which):
    return next(r for r in outcome.stage_results if r.stage is which)


def expected_status(failed: set[str]) -> StageStatus:
    ok = set(PERSPECTIVE_SEATS) - failed
    if not failed:
        return StageStatus.COMPLETED
    if len(ok) >= 3 and ok & REQUIRED_ANY:
        return StageStatus.DEGRADED
    return StageStatus.FAILED


def assert_absence(outcome, failed: set[str]):
    """The invariants that must hold for every failure combination."""
    perspectives = perspectives_of(outcome)
    assert set(perspectives) == set(PERSPECTIVE_SEATS) - failed
    r1 = stage(outcome, P)
    results = {r.seat_id: r for r in r1.seat_results}
    for seat in failed:
        assert results[seat].payload is None and not results[seat].ok
        assert seat not in {a.author for a in r1.artifacts}
        assert seat not in perspectives  # never substituted by, or defaulted to, anything
    assert outcome.answer is None  # an exploration run never has an answer
    return r1


# ── every subset of failing seats (the quorum arithmetic) ───────────────────

SUBSETS = [set(c) for n in range(6) for c in itertools.combinations(PERSPECTIVE_SEATS, n)]


@pytest.mark.parametrize("failed", SUBSETS, ids=lambda s: "+".join(sorted(s)) or "none")
def test_every_combination_of_seat_failures_obeys_quorum_and_leaves_absences(failed):
    invoker = fake(dict.fromkeys(failed, SeatStatus.TIMEOUT))
    outcome = explore_fake(invoker)
    r1 = assert_absence(outcome, failed)
    want = expected_status(failed)
    assert r1.status is want
    if want is StageStatus.COMPLETED:
        assert outcome.status is OutcomeStatus.COMPLETED and r1.degradations == ()
    elif want is StageStatus.DEGRADED:
        assert outcome.status is OutcomeStatus.DEGRADED
        assert set(r1.degradations) == {f"seat_unavailable:{s}" for s in failed}
    else:
        assert outcome.status is OutcomeStatus.FAILED
        assert "quorum_not_met" in r1.degradations
        assert outcome.failure_summary and outcome.failure_summary.startswith("perspectives failed")


@pytest.mark.parametrize("seat", PERSPECTIVE_SEATS)
@pytest.mark.parametrize("status", NON_OK, ids=lambda s: s.value)
def test_one_seat_failing_any_way_degrades_the_run_and_leaves_an_absence(seat, status):
    outcome = explore_fake(fake({seat: status}))
    r1 = assert_absence(outcome, {seat})
    assert r1.status is StageStatus.DEGRADED and outcome.status is OutcomeStatus.DEGRADED
    result = next(r for r in r1.seat_results if r.seat_id == seat)
    assert result.status is status and result.error is not None


def test_skeptic_and_fact_checker_both_down_fails_even_with_three_seats_ok():
    outcome = explore_fake(fake({"skeptic": SeatStatus.TIMEOUT, "fact_checker": SeatStatus.EMPTY}))
    assert len(perspectives_of(outcome)) == 3  # analyst, creative, practicalist delivered
    assert stage(outcome, P).status is StageStatus.FAILED
    assert outcome.status is OutcomeStatus.FAILED and outcome.answer is None


def test_one_of_the_required_pair_is_enough():
    outcome = explore_fake(fake({"skeptic": SeatStatus.TIMEOUT, "creative": SeatStatus.TIMEOUT}))
    assert stage(outcome, P).status is StageStatus.DEGRADED
    assert set(perspectives_of(outcome)) == {"analyst", "fact_checker", "practicalist"}


def test_every_seat_down_is_a_failed_run_with_no_answer_and_no_evidence():
    outcome = explore_fake(fake(dict.fromkeys(PERSPECTIVE_SEATS, SeatStatus.PROVIDER_ERROR)))
    assert outcome.status is OutcomeStatus.FAILED and outcome.answer is None
    assert perspectives_of(outcome) == {} and outcome.failure_summary


def test_failure_text_lives_only_in_the_failure_summary_never_an_answer():
    outcome = explore_fake(fake(dict.fromkeys(PERSPECTIVE_SEATS, SeatStatus.TIMEOUT)))
    assert outcome.answer is None and outcome.artifacts == ()
    assert "perspectives failed" in outcome.failure_summary


# ── Moderator failure: R0 never blocks R1 ───────────────────────────────────


@pytest.mark.parametrize("status", NON_OK, ids=lambda s: s.value)
def test_moderator_failure_degrades_but_r1_still_runs_on_the_deterministic_frame(status):
    invoker = fake(moderator=status)
    sink = Sink()
    request = make_request(question="Which database should we pick?")
    outcome = explore_fake(invoker, request, sink)
    assert stage(outcome, F).status is StageStatus.DEGRADED
    assert stage(outcome, P).status is StageStatus.COMPLETED
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is None
    assert set(perspectives_of(outcome)) == set(PERSPECTIVE_SEATS)
    frame = frame_of(outcome, request)
    assert frame.degraded and frame.question_restated == "Which database should we pick?"
    assert TraceEventKind.FRAME_FALLBACK in sink.kinds()
    for call in (c for c in invoker.calls if c.stage is P):
        user = call.messages[1].content
        assert "Which database should we pick?" in user  # the deterministic frame, nothing more
        assert "problem_type" in user and '"other"' in user


def test_a_failed_moderator_and_failed_perspectives_together_still_fail_the_run():
    outcome = explore_fake(
        fake(dict.fromkeys(PERSPECTIVE_SEATS, SeatStatus.TIMEOUT), moderator=SeatStatus.TIMEOUT)
    )
    assert outcome.status is OutcomeStatus.FAILED and outcome.answer is None


def test_a_stage_bug_in_r0_degrades_it_and_r1_falls_back_to_the_deterministic_frame():
    outcome = explore_fake(fake(moderator=RuntimeError("bug")))
    assert stage(outcome, F).degradations == ("stage_error:RuntimeError",)
    assert stage(outcome, F).artifacts == ()
    assert stage(outcome, P).status is StageStatus.COMPLETED
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is None


# ── malformed output ────────────────────────────────────────────────────────


def test_a_seat_malformed_twice_is_an_unparseable_absence_after_exactly_one_repair():
    invoker = fake({"creative": ["not json", "still not json"]})
    outcome = explore_fake(invoker)
    r1 = assert_absence(outcome, {"creative"})
    result = next(r for r in r1.seat_results if r.seat_id == "creative")
    assert result.status is SeatStatus.UNPARSEABLE and result.attempts == 2
    assert len(invoker.calls_for("creative", P)) == 2  # one repair, never a loop
    assert outcome.status is OutcomeStatus.DEGRADED


def test_a_repaired_seat_is_a_normal_perspective_and_does_not_degrade_the_run():
    outcome = explore_fake(fake({"skeptic": ["{broken", valid_perspective_json("skeptic")]}))
    assert "skeptic" in perspectives_of(outcome)
    assert outcome.status is OutcomeStatus.COMPLETED
    result = next(r for r in stage(outcome, P).seat_results if r.seat_id == "skeptic")
    assert result.ok and result.attempts == 2


def test_several_seats_malformed_at_once_follow_the_same_quorum_rules():
    bad = ["nope", "nope again"]
    outcome = explore_fake(fake({"analyst": bad, "creative": bad, "practicalist": bad}))
    assert set(perspectives_of(outcome)) == {"skeptic", "fact_checker"}
    assert stage(outcome, P).status is StageStatus.FAILED  # only two delivered
    assert outcome.status is OutcomeStatus.FAILED and outcome.answer is None


def test_a_perspective_that_is_valid_json_but_wrong_in_shape_is_not_accepted():
    shapeless = '{"position": "yes"}'
    outcome = explore_fake(fake({"analyst": [shapeless, shapeless]}))
    assert "analyst" not in perspectives_of(outcome)


def test_a_failed_repair_at_the_provider_keeps_its_typed_status():
    outcome = explore_fake(fake({"fact_checker": ["garbage", SeatStatus.AUTH_ERROR]}))
    result = next(r for r in stage(outcome, P).seat_results if r.seat_id == "fact_checker")
    assert result.status is SeatStatus.AUTH_ERROR and result.payload is None


# ── a bug inside one seat's job ─────────────────────────────────────────────


def test_a_seat_job_bug_is_a_visible_typed_failure_not_a_crash_or_a_success():
    invoker = fake({"practicalist": RuntimeError("the secret internal detail")})
    sink = Sink()
    outcome = explore_fake(invoker, sink=sink)
    r1 = assert_absence(outcome, {"practicalist"})
    result = next(r for r in r1.seat_results if r.seat_id == "practicalist")
    assert result.status is SeatStatus.PROVIDER_ERROR
    assert result.error.message == "internal_error:RuntimeError"
    assert "secret internal detail" not in canonical_json(outcome)
    errors = [e for e in sink.items if e.event is TraceEventKind.SEAT_ERROR]
    assert [(e.seat, e.detail) for e in errors] == [("practicalist", "RuntimeError")]
    assert outcome.status is OutcomeStatus.DEGRADED


# ── the failure statuses recorded on the trace ──────────────────────────────


def test_seat_result_events_record_each_seats_status_and_nothing_else():
    sink = Sink()
    explore_fake(fake({"creative": SeatStatus.TIMEOUT}), sink=sink)
    seen = {
        e.seat: e.status
        for e in sink.items
        if e.event is TraceEventKind.SEAT_RESULT and e.stage is P
    }
    assert seen == {
        "analyst": "ok",
        "skeptic": "ok",
        "creative": "timeout",
        "fact_checker": "ok",
        "practicalist": "ok",
    }
    assert all(len(e.detail) <= 300 and "restated" not in e.detail for e in sink.items)


# ── wire level: real adapter, real agent base class, scripted provider ──────


def wire(scripts=None, request=None, **runtime):
    provider = ScriptedProvider(scripts)
    invoker = runtime.pop("invoker", None) or runtime_invoker(provider, **runtime)
    outcome = run(explore(invoker, request or make_request()))
    return provider, outcome


def test_wire_provider_errors_are_absences_and_the_rest_still_deliver():
    scripts = {
        "creative": [ProviderConnectionError("down")],
        "analyst": [ProviderConnectionError("x")],
    }
    provider, outcome = wire(scripts)
    assert_absence(outcome, {"creative", "analyst"})
    assert outcome.status is OutcomeStatus.DEGRADED
    statuses = {r.seat_id: r.status for r in stage(outcome, P).seat_results}
    assert statuses["creative"] is SeatStatus.PROVIDER_ERROR


def test_wire_auth_errors_are_typed_and_mark_the_key(monkeypatch):
    marked = []
    from velune.providers import keystore

    monkeypatch.setattr(keystore, "mark_invalid", lambda pid, reason="": marked.append(pid))
    provider, outcome = wire({"skeptic": [ProviderAuthenticationError("bad key")]})
    result = next(r for r in stage(outcome, P).seat_results if r.seat_id == "skeptic")
    assert result.status is SeatStatus.AUTH_ERROR and result.payload is None
    assert marked and set(marked) == {"fake"}


def test_wire_a_slow_seat_times_out_per_its_call_budget():
    request = make_request(settings=CouncilSettings(seat_timeout_s=0.1))
    provider, outcome = wire({"practicalist": [Delay(5)]}, request)
    result = next(r for r in stage(outcome, P).seat_results if r.seat_id == "practicalist")
    assert result.status is SeatStatus.TIMEOUT and result.payload is None
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is None


def test_wire_empty_replies_are_absences():
    provider, outcome = wire({"analyst": ["   "]})
    result = next(r for r in stage(outcome, P).seat_results if r.seat_id == "analyst")
    assert result.status is SeatStatus.EMPTY and "analyst" not in perspectives_of(outcome)


def _bad_then_good(seat: str):
    state = {"n": 0}

    def reply(request):
        state["n"] += 1
        return "this is not json" if state["n"] == 1 else valid_perspective_json(seat, "rep")

    return reply


def test_wire_repair_request_carries_only_the_seats_own_reply_and_the_errors():
    provider, outcome = wire({"analyst": [_bad_then_good("analyst")]})
    first, second = provider.requests_for("analyst")
    assert [m["role"] for m in second.messages] == ["system", "user", "assistant", "user"]
    assert second.messages[:2] == first.messages
    assert second.messages[2]["content"] == "this is not json"
    for peer in PERSPECTIVE_SEATS:
        assert sentinel(peer, "") not in "".join(m["content"] for m in second.messages)
    assert perspectives_of(outcome)["analyst"].claims[0].id == "AN-1"
    result = next(r for r in stage(outcome, P).seat_results if r.seat_id == "analyst")
    assert result.attempts == 2 and outcome.status is OutcomeStatus.COMPLETED


# ── fallback ────────────────────────────────────────────────────────────────


def fallback_setup(primary_fails_for=PERSPECTIVE_SEATS, backup_scripts=None):
    primary_model = make_model("m-primary", "p1")
    backup_model = make_model("m-backup", "p2")

    def p1_reply(seat):
        def reply(request):
            return ProviderConnectionError("p1 down")

        return reply

    p1 = ScriptedProvider({seat: [p1_reply(seat)] for seat in primary_fails_for}, provider_id="p1")
    p2 = ScriptedProvider(backup_scripts, provider_id="p2")
    invoker = runtime_invoker(
        p1,
        ChainMapper(primary_model, [backup_model]),
        extra=(p2,),
        fallback_provider_ids=("p2",),
        allow_cloud_fallback_from_local=True,
    )
    return p1, p2, invoker


def test_fallback_answers_are_perspectives_recorded_with_the_model_that_gave_them():
    sink = Sink()
    p1, p2, invoker = fallback_setup()
    outcome = run(explore(invoker, make_request(), sink=sink))
    assert set(perspectives_of(outcome)) == set(PERSPECTIVE_SEATS)
    results = stage(outcome, P).seat_results
    assert all(r.fallback_used and r.attempts == 2 for r in results)
    assert {(r.model.provider_id, r.model.model_id) for r in results} == {("p2", "m-backup")}
    assert outcome.status is OutcomeStatus.COMPLETED  # a model fallback is not degradation
    fallbacks = [e for e in sink.items if e.event is TraceEventKind.SEAT_FALLBACK]
    assert [e.seat for e in fallbacks if e.stage is P] == list(PERSPECTIVE_SEATS)
    assert all(e.detail == "p2/m-backup" for e in fallbacks)


def test_fallback_applies_per_seat_so_only_failing_seats_switch_models():
    p1, p2, invoker = fallback_setup(primary_fails_for=("skeptic",))
    outcome = run(explore(invoker, make_request()))
    results = {r.seat_id: r for r in stage(outcome, P).seat_results}
    assert results["skeptic"].fallback_used and results["skeptic"].model.provider_id == "p2"
    assert not results["analyst"].fallback_used and results["analyst"].model.provider_id == "p1"
    assert outcome.status is OutcomeStatus.COMPLETED


def test_the_moderator_can_fall_back_too_and_the_frame_is_not_degraded():
    p1 = ScriptedProvider({"moderator": [ProviderConnectionError("down")]}, provider_id="p1")
    p2 = ScriptedProvider(provider_id="p2")
    invoker = runtime_invoker(
        p1,
        ChainMapper(make_model("m-primary", "p1"), [make_model("m-backup", "p2")]),
        extra=(p2,),
        fallback_provider_ids=("p2",),
        allow_cloud_fallback_from_local=True,
    )
    outcome = run(explore(invoker, make_request()))
    assert frame_of(outcome, make_request()).degraded is False
    assert stage(outcome, F).status is StageStatus.COMPLETED


def test_exhausted_fallbacks_are_absences_not_successes():
    primary_model = make_model("m-primary", "p1")
    p1 = ScriptedProvider(
        {s: [ProviderConnectionError("p1")] for s in PERSPECTIVE_SEATS}, provider_id="p1"
    )
    p2 = ScriptedProvider(
        {s: [ProviderConnectionError("p2")] for s in PERSPECTIVE_SEATS}, provider_id="p2"
    )
    invoker = runtime_invoker(
        p1,
        ChainMapper(primary_model, [make_model("m-backup", "p2")]),
        extra=(p2,),
        fallback_provider_ids=("p2",),
        allow_cloud_fallback_from_local=True,
    )
    outcome = run(explore(invoker, make_request()))
    assert outcome.status is OutcomeStatus.FAILED and outcome.answer is None
    assert perspectives_of(outcome) == {}


def test_without_a_fallback_allow_list_a_failed_seat_is_simply_absent():
    p1 = ScriptedProvider({"creative": [ProviderConnectionError("down")]}, provider_id="p1")
    invoker = runtime_invoker(p1, FakeMapper(make_model("m1", "p1")))
    outcome = run(explore(invoker, make_request()))
    assert "creative" not in perspectives_of(outcome)
    assert outcome.status is OutcomeStatus.DEGRADED


# ── cancellation ────────────────────────────────────────────────────────────


def _cancel_when_in_flight(scripts, seats_in_flight, settings=None):
    async def scenario():
        provider = ScriptedProvider(scripts)
        invoker = runtime_invoker(provider)
        request = make_request(settings=settings) if settings else make_request()
        task = asyncio.ensure_future(explore(invoker, request))
        while not all(provider.requests_for(s) for s in seats_in_flight):
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(CouncilCancelled) as info:
            await task
        await asyncio.sleep(0.05)
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return info.value.outcome, provider, leftovers

    return run(scenario())


def test_cancellation_mid_r1_keeps_the_frame_and_leaves_nothing_running():
    outcome, provider, leftovers = _cancel_when_in_flight({"analyst": [Delay(30)]}, ["analyst"])
    assert outcome.status is OutcomeStatus.CANCELLED and outcome.answer is None
    assert outcome.failure_summary == "cancelled during perspectives"
    assert [r.stage for r in outcome.stage_results] == [F]  # completed evidence retained
    assert outcome.stage_results[0].status is StageStatus.COMPLETED
    assert frame_of(outcome, make_request()).degraded is False
    assert perspectives_of(outcome) == {}
    assert leftovers == []
    # seats after the cancelled one were never called
    assert not provider.requests_for("skeptic")


def test_cancellation_with_every_seat_in_flight_cancels_them_all():
    scripts = {seat: [Delay(30)] for seat in PERSPECTIVE_SEATS}
    outcome, provider, leftovers = _cancel_when_in_flight(
        scripts, PERSPECTIVE_SEATS, CouncilSettings(max_concurrency=5)
    )
    assert outcome.status is OutcomeStatus.CANCELLED and outcome.answer is None
    assert leftovers == []
    assert all(len(provider.requests_for(s)) == 1 for s in PERSPECTIVE_SEATS)


def test_cancellation_mid_r0_retains_nothing_and_carries_no_answer():
    outcome, provider, leftovers = _cancel_when_in_flight({"moderator": [Delay(30)]}, ["moderator"])
    assert outcome.status is OutcomeStatus.CANCELLED and outcome.answer is None
    assert outcome.failure_summary == "cancelled during frame"
    assert outcome.stage_results == () and leftovers == []
    assert not provider.requests_for("analyst")


def test_a_cancelled_run_never_looks_successful():
    outcome, _, _ = _cancel_when_in_flight({"analyst": [Delay(30)]}, ["analyst"])
    assert not outcome.succeeded and outcome.artifacts == ()


# ── an unrelated provider sanity check: the doubles themselves ──────────────


def test_the_scripted_provider_double_records_every_request():
    provider, outcome = wire()
    assert len(provider.log) == 6 and outcome.status is OutcomeStatus.COMPLETED
    assert isinstance(provider, FakeProvider)
