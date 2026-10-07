"""R3 revision: each seat sees only its own work and the critiques addressed to it.

Tier A drives the stage with a scripted seat invoker; Tier B runs the runtime adapter over a scripted
provider so the requests inspected are the ones a model would receive.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from tests.council_core_fakes import FakeSeatInvoker
from tests.council_scripted import (
    PERSPECTIVE_SEATS,
    make_request,
    reaffirming_reply,
    review_reply,
    revision_reply,
    sentinel,
    valid_frame_json,
    valid_perspective_json,
)
from tests.council_wire import (
    UP_TO_REVIEW,
    UP_TO_REVISION,
    ScriptedProvider,
    deliberate,
    deliberation_runner,
    runtime_invoker,
    wire_revision_reply,
)
from velune.cognition.execution_trace import CallReason, trace_request
from velune.council.adapters.runtime import RequestTraceSink
from velune.council.contracts import ChangeKind, RevisionStatus
from velune.council.domain import ArtifactKind, StageId
from velune.council.profiles import GENERAL_PROFILE
from velune.council.request import CouncilSettings
from velune.council.results import OutcomeStatus, SeatStatus, StageStatus
from velune.council.runner import CouncilCancelled
from velune.council.seatflow import STAGE_DEADLINE
from velune.council.serialization import canonical_json
from velune.council.stages import VisibilityPolicy
from velune.council.state import StageView
from velune.council.topology import reviewers_of
from velune.council.trace import TraceEventKind

F, P, R, V = StageId.FRAME, StageId.PERSPECTIVES, StageId.REVIEW, StageId.REVISION
NON_OK = [s for s in SeatStatus if s is not SeatStatus.OK]
PREFIX = {s.id: s.claim_prefix for s in GENERAL_PROFILE.perspective_seats}


def run(coro, timeout: float = 30):
    return asyncio.run(asyncio.wait_for(coro, timeout))


class Events:
    def __init__(self) -> None:
        self.items = []

    def emit(self, event) -> None:
        self.items.append(event)

    def of(self, kind):
        return [e for e in self.items if e.event is kind]


def scripted(tags=None, review_tag="", revision_tag="", overrides=None) -> FakeSeatInvoker:
    invoker = FakeSeatInvoker()
    invoker.script("moderator", F, valid_frame_json())
    for seat in PERSPECTIVE_SEATS:
        invoker.script(seat, P, valid_perspective_json(seat, (tags or {}).get(seat, "t")))
        invoker.script(seat, R, review_reply(review_tag))
        invoker.script(seat, V, revision_reply(revision_tag))
    for (seat, stage), entries in (overrides or {}).items():
        invoker.script(seat, stage, *entries)
    return invoker


def go(invoker, request=None, plan=UP_TO_REVISION, **kw):
    events = Events()
    outcome = run(deliberate(invoker, request, sink=events, plan=plan, **kw))
    return outcome, events


def stage_of(outcome, stage):
    return next(s for s in outcome.stage_results if s.stage is stage)


def revisions(outcome):
    return {r.seat_id: r.payload for r in stage_of(outcome, V).seat_results if r.ok}


def results_of(outcome):
    return {r.seat_id: r for r in stage_of(outcome, V).seat_results}


def v_calls(invoker):
    return [c for c in invoker.calls if c.stage is V]


def user_of(call) -> str:
    return call.messages[1].content


def critique_mark(reviewer, target, tag=""):
    return sentinel(f"{reviewer}-on-{target}", tag)


# ── a healthy revision ──────────────────────────────────────────────────────


def test_a_healthy_run_revises_every_seat_once_and_still_has_no_answer():
    invoker = scripted()
    outcome, _ = go(invoker)
    assert [s.stage for s in outcome.stage_results] == [F, P, R, V]
    assert all(s.status is StageStatus.COMPLETED for s in outcome.stage_results)
    assert outcome.status is OutcomeStatus.COMPLETED
    assert outcome.answer is None and outcome.artifacts == ()
    calls = v_calls(invoker)
    assert sorted(c.seat_id for c in calls) == sorted(PERSPECTIVE_SEATS)
    assert all(c.reason is CallReason.REVISION and c.stage is V for c in calls)
    stage = stage_of(outcome, V)
    assert sorted(a.author for a in stage.artifacts) == sorted(PERSPECTIVE_SEATS)
    assert all(a.kind is ArtifactKind.REVISION and a.target is None for a in stage.artifacts)


def test_each_revision_is_a_valid_patch_on_the_seats_own_perspective():
    outcome, _ = go(scripted())
    r1 = {r.seat_id: r.payload for r in stage_of(outcome, P).seat_results}
    for seat, revision in revisions(outcome).items():
        assert revision.seat_id == seat and revision.status is RevisionStatus.REVISED
        assert [c.id for c in revision.claims] == [c.id for c in r1[seat].claims]
        (change,) = revision.changes
        assert change.claim_id == f"{PREFIX[seat]}-1" and change.change is ChangeKind.MODIFIED
        assert revision.claims[1:] == r1[seat].claims[1:]  # nothing edited, nothing changed
        assert revision.revised_confidence == 0.5 and revision.confidence_delta == pytest.approx(
            -0.05
        )


def test_revising_never_rewrites_the_originals():
    reviewed, _ = go(scripted(), plan=UP_TO_REVIEW)
    revised, _ = go(scripted())
    for stage in (P, R):
        assert canonical_json(stage_of(reviewed, stage)) == canonical_json(stage_of(revised, stage))


def test_the_outcome_is_deterministic():
    one, _ = go(scripted())
    two, _ = go(scripted())
    assert canonical_json(one) == canonical_json(two)


# ── what each seat is shown ─────────────────────────────────────────────────


@pytest.mark.parametrize("seat", PERSPECTIVE_SEATS)
def test_a_seat_sees_its_own_perspective_and_only_the_critiques_addressed_to_it(seat):
    invoker = scripted()
    go(invoker)
    (call,) = [c for c in v_calls(invoker) if c.seat_id == seat]
    user = user_of(call)
    assert sentinel(seat, "t") in user  # its own R1 work
    mine = set(reviewers_of(GENERAL_PROFILE, seat))
    for reviewer in PERSPECTIVE_SEATS:
        if reviewer == seat:
            continue
        shown = critique_mark(reviewer, seat) in user
        assert shown is (reviewer in mine), (seat, reviewer)
        assert (f'<critique from="{reviewer}"' in user) is (reviewer in mine)
    for other in PERSPECTIVE_SEATS:  # critiques addressed to anyone else are never shown
        for reviewer in PERSPECTIVE_SEATS:
            if other != seat and reviewer != other:
                assert critique_mark(reviewer, other) not in user
    for peer in PERSPECTIVE_SEATS:  # and no peer perspective, in any form
        if peer != seat:
            assert sentinel(peer, "t") not in user


def test_the_system_prompt_lists_exactly_the_objections_that_need_an_answer():
    invoker = scripted()
    go(invoker)
    (call,) = [c for c in v_calls(invoker) if c.seat_id == "analyst"]
    system = call.messages[0].content
    assert '<required_responses>[{"objection":1,"reviewer":"skeptic"}' in system.replace(" ", "")
    assert "fact_checker" in system and '<stage id="revision"/>' in system
    assert "<schema>" in system and '"responses"' in system and '"edits"' in system


def test_evidence_and_the_frame_reach_every_seat_identically():
    from velune.council.request import EvidenceItem

    request = make_request(evidence=(EvidenceItem(id="e1", text="EV-9"),))
    invoker = scripted()
    go(invoker, request)
    for call in v_calls(invoker):
        assert user_of(call).count("EV-9") == 1 and "<frame>" in user_of(call)


def test_a_seats_request_does_not_depend_on_other_seats_revisions():
    one, two = scripted(revision_tag="a"), scripted(revision_tag="b")
    go(one)
    go(two)
    for first, second in zip(v_calls(one), v_calls(two), strict=True):
        assert first.messages == second.messages  # R3 inputs are R1 and R2 only


def test_a_seats_request_changes_only_with_critiques_addressed_to_it():
    base = scripted()
    other = scripted(
        overrides={("creative", R): [review_reply("u")]}
    )  # creative reviews skeptic/practicalist
    go(base)
    go(other)

    def by_seat(invoker):
        return {c.seat_id: c.messages for c in v_calls(invoker)}

    before, after = by_seat(base), by_seat(other)
    assert before["analyst"] == after["analyst"]  # creative never reviews the analyst
    assert before["fact_checker"] == after["fact_checker"]
    assert before["skeptic"] != after["skeptic"] and before["practicalist"] != after["practicalist"]


def test_requests_are_independent_of_scheduling():
    baseline = scripted()
    go(baseline)
    for limit in (1, 5):
        other = scripted()
        go(other, make_request(settings=CouncilSettings(max_concurrency=limit)))
        assert {c.seat_id: c.messages for c in v_calls(other)} == {
            c.seat_id: c.messages for c in v_calls(baseline)
        }


def test_the_stage_reads_only_its_own_perspective_and_critiques_aimed_at_it():
    log = []

    class Logged(StageView):
        def read(self, kind, *, seat=None):
            items = super().read(kind, seat=seat)
            log.append((self.viewer, kind, tuple((i.author, i.target) for i in items)))
            return items

        def digest(self, kind):
            log.append((self.viewer, kind, "digest"))
            return super().digest(kind)

    from velune.council.revision import RevisionStage

    class Recording(RevisionStage):
        async def run(self, ctx):
            original = ctx.view_factory

            def make(seat_id):
                v = original(seat_id)
                return Logged(
                    policy=v._policy,
                    stage=v._stage,
                    viewer=v._viewer,
                    request=v._request,
                    store=v._store,
                    assignments=v._assignments,
                )

            return await super().run(dataclasses.replace(ctx, view_factory=make))

    from tests.council_scripted import StaticPrompts
    from velune.council.frame import FrameStage
    from velune.council.perspectives import PerspectiveStage
    from velune.council.review import ReviewStage

    prompts = StaticPrompts()
    runner = deliberation_runner(
        scripted(),
        stages=[
            FrameStage(prompts),
            PerspectiveStage(prompts),
            ReviewStage(prompts),
            Recording(prompts),
        ],
    )
    run(runner.run(make_request(), plan=UP_TO_REVISION))
    assert {kind for _, kind, _ in log} <= set(VisibilityPolicy().matrix()[V])
    for viewer, kind, seen in log:
        assert seen != "digest"
        if kind is ArtifactKind.PERSPECTIVE:
            assert [a for a, _ in seen] == [viewer]
        if kind is ArtifactKind.CRITIQUE:
            assert all(target == viewer for _, target in seen)
    assert not {kind for _, kind, _ in log} & {
        ArtifactKind.REVISION,
        ArtifactKind.DECISION,
        ArtifactKind.ANSWER,
    }


# ── revision semantics ──────────────────────────────────────────────────────


def test_rejecting_every_objection_reaffirms_the_original_position():
    invoker = scripted(overrides={(seat, V): [reaffirming_reply()] for seat in PERSPECTIVE_SEATS})
    outcome, _ = go(invoker)
    r1 = {r.seat_id: r.payload for r in stage_of(outcome, P).seat_results}
    for seat, revision in revisions(outcome).items():
        assert revision.status is RevisionStatus.REAFFIRMED
        assert revision.claims == r1[seat].claims and revision.changes == ()
        assert revision.accepted == () and revision.rejected  # the rejection is recorded
    assert outcome.status is OutcomeStatus.COMPLETED


def test_a_partial_acceptance_is_recorded_as_accepted_with_its_edit():
    def partial(call):
        from tests.council_scripted import required_in

        required = required_in(call)
        prefix = PREFIX[call.seat_id]
        responses = [
            {**required[0], "decision": "partial", "note": "some of it"},
            *({**item, "decision": "reject", "note": "no"} for item in required[1:]),
        ]
        edits = [
            {
                "op": "modify",
                "claim_id": f"{prefix}-1",
                "confidence": 0.2,
                "reason": "weaker",
                "caused_by": [required[0]],
            }
        ]
        return json.dumps({"responses": responses, "edits": edits})

    outcome, _ = go(scripted(overrides={("analyst", V): [partial]}))
    revision = revisions(outcome)["analyst"]
    assert revision.changes[0].change is ChangeKind.WEAKENED
    assert len(revision.accepted) == 1 and len(revision.rejected) == 1


def test_insufficient_evidence_is_recorded_as_deferred_and_changes_nothing():
    def defer(call):
        from tests.council_scripted import required_in

        return json.dumps(
            {
                "responses": [
                    {**i, "decision": "insufficient_evidence", "note": "unclear"}
                    for i in required_in(call)
                ]
            }
        )

    outcome, _ = go(scripted(overrides={("skeptic", V): [defer]}))
    revision = revisions(outcome)["skeptic"]
    assert revision.deferred and not revision.accepted and not revision.rejected
    assert revision.status is RevisionStatus.REAFFIRMED


# ── a seat that cannot or need not revise ───────────────────────────────────


@pytest.mark.parametrize("status", NON_OK)
@pytest.mark.parametrize("seat", PERSPECTIVE_SEATS)
def test_a_seat_that_fails_to_revise_is_an_absence_and_nothing_stands_in(seat, status):
    outcome, _ = go(scripted(overrides={(seat, V): [status]}))
    stage = stage_of(outcome, V)
    result = results_of(outcome)[seat]
    assert result.status is status and result.payload is None
    assert not [a for a in stage.artifacts if a.author == seat]
    assert set(revisions(outcome)) == set(PERSPECTIVE_SEATS) - {seat}
    assert stage.status is StageStatus.DEGRADED and f"seat_unavailable:{seat}" in stage.degradations
    r1 = {r.seat_id: r.payload for r in stage_of(outcome, P).seat_results}
    assert r1[seat] is not None  # its R1 perspective is untouched and still there
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is None


def test_a_seat_nobody_reviewed_is_not_asked_and_is_noted():
    overrides = {("skeptic", R): [SeatStatus.TIMEOUT], ("fact_checker", R): [SeatStatus.TIMEOUT]}
    invoker = scripted(overrides=overrides)
    outcome, _ = go(invoker)
    stage = stage_of(outcome, V)
    assert "no_critiques:analyst" in stage.degradations  # its only two reviewers failed
    assert not [c for c in v_calls(invoker) if c.seat_id == "analyst"]
    assert "analyst" not in results_of(outcome) and "analyst" not in revisions(outcome)
    assert stage.status is StageStatus.COMPLETED  # nothing it was asked to do failed
    assert outcome.status is OutcomeStatus.DEGRADED  # R2 already said so
    assert outcome.answer is None


def test_with_no_critiques_at_all_nobody_is_asked_and_the_run_is_degraded_by_r2():
    overrides = {(seat, R): [SeatStatus.PROVIDER_ERROR] for seat in PERSPECTIVE_SEATS}
    invoker = scripted(overrides=overrides)
    outcome, _ = go(invoker)
    stage = stage_of(outcome, V)
    assert v_calls(invoker) == [] and stage.seat_results == () and stage.artifacts == ()
    assert {d for d in stage.degradations if d.startswith("no_critiques")} == {
        f"no_critiques:{s}" for s in PERSPECTIVE_SEATS
    }
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is None


def test_a_seat_missing_from_r1_is_not_asked_to_revise():
    invoker = scripted(overrides={("practicalist", P): [SeatStatus.TIMEOUT]})
    outcome, _ = go(invoker)
    assert not [c for c in v_calls(invoker) if c.seat_id == "practicalist"]
    assert "practicalist" not in revisions(outcome)
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is None


# ── malformed and invalid revisions: one repair, then an absence ────────────


def test_an_invalid_revision_earns_exactly_one_same_seat_repair():
    invoker = scripted(overrides={("analyst", V): ["{}", revision_reply("t")]})
    outcome, events = go(invoker)
    calls = [c for c in v_calls(invoker) if c.seat_id == "analyst"]
    assert len(calls) == 2 and calls[1].reason is CallReason.VALIDATION_FAILURE
    first, second = calls
    assert second.messages[: len(first.messages)] == first.messages
    extra = second.messages[len(first.messages) :]
    assert [m.role for m in extra] == ["assistant", "user"] and extra[0].content == "{}"
    assert "needs a response" in extra[1].content
    for mark in (sentinel("skeptic", "t"), critique_mark("creative", "skeptic")):
        assert mark not in "".join(m.content for m in extra)
    assert results_of(outcome)["analyst"].ok and results_of(outcome)["analyst"].attempts == 2
    assert events.of(TraceEventKind.SEAT_REPAIR)[0].seat == "analyst"


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "{}",  # unanswered objections
        '{"responses": [], "edits": []}',
        json.dumps({"seat_id": "skeptic"}),  # a field the model may not set
    ],
)
def test_a_revision_that_stays_invalid_is_an_absence_after_exactly_one_repair(bad):
    invoker = scripted(overrides={("fact_checker", V): [bad]})
    outcome, _ = go(invoker)
    assert len([c for c in v_calls(invoker) if c.seat_id == "fact_checker"]) == 2
    result = results_of(outcome)["fact_checker"]
    assert result.status is SeatStatus.UNPARSEABLE and result.payload is None
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is None


def test_a_change_without_an_accepted_cause_is_invalid():
    def sneaky(call):
        from tests.council_scripted import required_in

        required = required_in(call)
        return json.dumps(
            {
                "responses": [{**i, "decision": "reject", "note": "no"} for i in required],
                "edits": [
                    {
                        "op": "modify",
                        "claim_id": f"{PREFIX[call.seat_id]}-1",
                        "confidence": 0.1,
                        "reason": "just because",
                        "caused_by": [required[0]],
                    }
                ],
            }
        )

    outcome, _ = go(scripted(overrides={("creative", V): [sneaky]}))
    assert results_of(outcome)["creative"].status is SeatStatus.UNPARSEABLE


def test_a_revision_that_cites_another_seats_claim_is_invalid():
    leak = revision_reply("t", position="as CR-1 already said")
    outcome, _ = go(scripted(overrides={("analyst", V): [leak]}))
    assert results_of(outcome)["analyst"].status is SeatStatus.UNPARSEABLE


def test_a_job_that_raised_is_a_visible_typed_failure():
    outcome, events = go(scripted(overrides={("practicalist", V): [RuntimeError("boom")]}))
    result = results_of(outcome)["practicalist"]
    assert result.status is SeatStatus.PROVIDER_ERROR and result.payload is None
    assert result.error.message == "internal_error:RuntimeError"
    (error,) = events.of(TraceEventKind.SEAT_ERROR)
    assert error.seat == "practicalist" and error.stage is V


# ── time and cancellation ───────────────────────────────────────────────────


class SlowRevision(FakeSeatInvoker):
    def __init__(self, slow, base):
        super().__init__()
        self.scripts = base.scripts
        self.slow = set(slow)
        self.started: list[str] = []

    async def invoke(self, call):
        if call.stage is V:
            self.started.append(call.seat_id)
            if call.seat_id in self.slow:
                await asyncio.sleep(30)
        return await super().invoke(call)


def test_revisions_that_finished_survive_the_stage_deadline():
    invoker = SlowRevision(("fact_checker", "practicalist"), scripted())
    request = make_request(settings=CouncilSettings(wall_budget_s=1.0, max_concurrency=5))
    outcome, _ = go(invoker, request)
    stage = stage_of(outcome, V)
    statuses = {r.seat_id: r.status for r in stage.seat_results}
    assert (
        statuses["fact_checker"] is SeatStatus.TIMEOUT
        and statuses["practicalist"] is SeatStatus.TIMEOUT
    )
    assert set(revisions(outcome)) == {"analyst", "skeptic", "creative"}
    assert STAGE_DEADLINE in stage.degradations and stage.status is StageStatus.DEGRADED
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is None


def test_cancellation_mid_revision_keeps_the_earlier_stages_and_leaves_nothing_running():
    invoker = SlowRevision(("fact_checker",), scripted())
    runner = deliberation_runner(invoker)

    async def main():
        task = asyncio.ensure_future(runner.run(make_request(), plan=UP_TO_REVISION))
        for _ in range(400):
            if "fact_checker" in invoker.started:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(CouncilCancelled) as info:
            await task
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return info.value.outcome, leftovers

    outcome, leftovers = asyncio.run(asyncio.wait_for(main(), 30))
    assert outcome.status is OutcomeStatus.CANCELLED and outcome.answer is None
    assert outcome.failure_summary == "cancelled during revision"
    assert [s.stage for s in outcome.stage_results] == [F, P, R]
    assert leftovers == []


# ── the provider level ──────────────────────────────────────────────────────


def wire(scripts=None, tag=""):
    provider = ScriptedProvider(scripts or {}, tag=tag)

    async def scenario():
        with trace_request("req-r3") as trace:
            outcome = await deliberate(
                runtime_invoker(provider),
                make_request(),
                sink=RequestTraceSink(),
                plan=UP_TO_REVISION,
            )
        return trace, outcome

    trace, outcome = run(scenario())
    return provider, trace, outcome


def test_the_requests_a_model_receives_for_revision_carry_only_own_work_and_own_critiques():
    provider, _, outcome = wire()
    assert outcome.status is OutcomeStatus.COMPLETED and outcome.answer is None
    assert len(provider.log) == 16  # frame, five perspectives, five reviews, five revisions
    for seat in PERSPECTIVE_SEATS:
        (request,) = provider.requests_for(seat, "revision")
        body = json.dumps(request.messages, ensure_ascii=False)
        assert sentinel(seat, "") in body
        for peer in PERSPECTIVE_SEATS:
            if peer != seat:
                assert sentinel(peer, "") not in body
        for other in PERSPECTIVE_SEATS:
            for reviewer in PERSPECTIVE_SEATS:
                if other != seat and reviewer != other:
                    assert critique_mark(reviewer, other) not in body


def test_a_seats_wire_request_is_byte_identical_whatever_the_other_revisions_say():
    one, _, _ = wire({"analyst:revision": [wire_revision_reply("a")]})
    two, _, _ = wire({"analyst:revision": [wire_revision_reply("b")]})
    for seat in PERSPECTIVE_SEATS:
        assert (
            one.requests_for(seat, "revision")[0].messages
            == two.requests_for(seat, "revision")[0].messages
        )


def test_no_provider_call_across_the_four_stages_is_an_unexplained_repeat():
    provider, trace, outcome = wire({"skeptic:revision": ["not json", wire_revision_reply()]})
    assert outcome.status is OutcomeStatus.COMPLETED
    assert trace.unexplained_calls() == []
    reasons = [c.reason for c in trace.calls if c.seat == "skeptic"]
    assert reasons == [
        CallReason.PRIMARY,
        CallReason.REVIEW,
        CallReason.REVISION,
        CallReason.VALIDATION_FAILURE,
    ]
    kinds = {n.type.value for n in trace.nodes}
    assert {"framing", "perspectives", "review", "revision"} <= kinds
