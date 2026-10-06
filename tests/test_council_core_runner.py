"""The runner's guarantees, proven with scripted fake stages and a fake invoker/clock/ids."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from tests.council_core_fakes import CounterIds, FakeClock, FakeSeatInvoker
from velune.council.contracts import Artifact
from velune.council.domain import ArtifactKind, Depth, StageId
from velune.council.ports import SeatCall, SeatMessage
from velune.council.profiles import (
    ProfileNotRunnable,
    UnknownProfile,
    default_registry,
)
from velune.council.request import CouncilRequest, CouncilSettings
from velune.council.results import (
    OutcomeStatus,
    SeatResult,
    SeatStatus,
    StageStatus,
)
from velune.council.runner import (
    CouncilCancelled,
    StagedCouncilRunner,
)
from velune.council.stages import (
    STAGE_CONTRACTS,
    StageArtifact,
    StageContract,
    StageOutput,
)
from velune.council.state import StageContext
from velune.council.trace import TraceEventKind

ID = StageId
Hook = Callable[[StageContext, StageOutput], Awaitable[StageOutput] | StageOutput]


class SeatStage:
    """Calls every profile seat the contract allows, through the injected scheduler."""

    def __init__(
        self,
        stage: StageId,
        *,
        behave: Callable[[StageContext], Awaitable[StageOutput]] | None = None,
        post: Callable[[StageOutput], StageOutput] | None = None,
        contract: StageContract | None = None,
    ) -> None:
        self.contract = contract or STAGE_CONTRACTS[stage]
        self._behave = behave
        self._post = post

    async def run(self, ctx: StageContext) -> StageOutput:
        if self._behave is not None:
            return await self._behave(ctx)
        seats = [s for s in ctx.profile.all_seats if s.kind in ctx.contract.seat_kinds]

        def job(seat):
            async def go() -> SeatResult[str]:
                view = ctx.view_for(seat.id)
                view.question()
                return await ctx.invoker.invoke(
                    SeatCall(
                        seat_id=seat.id,
                        kind=seat.kind,
                        stage=ctx.contract.stage,
                        messages=(SeatMessage(role="user", content=view.question()),),
                        timeout_s=ctx.settings.seat_timeout_s,
                    )
                )

            return go

        outcomes = await ctx.scheduler.run(
            [job(s) for s in seats], max_concurrency=ctx.settings.max_concurrency
        )
        results = tuple(o for o in outcomes if isinstance(o, SeatResult))
        artifacts = tuple(
            StageArtifact(kind=ctx.contract.writes, author=r.seat_id, payload=r.payload)
            for r in results
            if r.ok
        )
        answer = None
        if ctx.contract.writes is ArtifactKind.ANSWER and results and results[0].ok:
            answer = results[0].payload
        output = StageOutput(seat_results=results, artifacts=artifacts, answer=answer)
        return self._post(output) if self._post else output


def full_stages(**overrides: SeatStage) -> dict[StageId, SeatStage]:
    stages = {stage: SeatStage(stage) for stage in STAGE_CONTRACTS}
    stages.update({s.contract.stage: s for s in overrides.values()})
    return stages


def make_runner(stages, invoker=None, clock=None, **kw) -> StagedCouncilRunner:
    return StagedCouncilRunner(
        registry=default_registry(),
        stages=stages,
        invoker=invoker or FakeSeatInvoker(),
        clock=clock or FakeClock(),
        ids=CounterIds(),
        **kw,
    )


def request(**kw) -> CouncilRequest:
    base = {"request_id": "req-1", "question": "Why is the sky blue?"}
    base.update(kw)
    return CouncilRequest(**base)


def run(coro, timeout: float = 15):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def by_stage(outcome):
    return {r.stage: r for r in outcome.stage_results}


# ── sequencing ──────────────────────────────────────────────────────────────


def test_full_run_executes_every_stage_in_order_and_completes():
    runner = make_runner(full_stages())
    outcome = run(runner.run(request()))
    assert [r.stage for r in outcome.stage_results] == list(STAGE_CONTRACTS)
    assert all(r.status is StageStatus.COMPLETED for r in outcome.stage_results)
    assert outcome.status is OutcomeStatus.COMPLETED
    assert outcome.answer == "synthesizer:synthesis"
    assert outcome.failure_summary is None
    assert outcome.profile_id == "general" and outcome.request_id == "req-1"


def test_quick_depth_runs_only_the_quick_plan():
    outcome = run(make_runner(full_stages()).run(request(depth=Depth.QUICK)))
    assert [r.stage for r in outcome.stage_results] == [
        ID.FRAME,
        ID.PERSPECTIVES,
        ID.ARBITRATION,
        ID.SYNTHESIS,
    ]


def test_only_the_synthesis_stage_may_produce_the_answer():
    def with_answer(output: StageOutput) -> StageOutput:
        return output.model_copy(update={"answer": "sneaky"})

    stages = full_stages(frame=SeatStage(ID.FRAME, post=with_answer))
    outcome = run(make_runner(stages).run(request()))
    frame = by_stage(outcome)[ID.FRAME]
    assert frame.status is StageStatus.DEGRADED  # optional stage, contract violated
    assert any("contract_violation" in d for d in frame.degradations)
    assert outcome.answer == "synthesizer:synthesis"


def test_seat_calls_reach_the_invoker_with_the_expected_fanout():
    invoker = FakeSeatInvoker()
    run(make_runner(full_stages(), invoker).run(request()))
    per_stage = {}
    for call in invoker.calls:
        per_stage.setdefault(call.stage, set()).add(call.seat_id)
    assert per_stage[ID.FRAME] == {"moderator"}
    assert per_stage[ID.PERSPECTIVES] == {
        "analyst",
        "skeptic",
        "creative",
        "fact_checker",
        "practicalist",
    }
    assert per_stage[ID.SYNTHESIS] == {"synthesizer"}


def test_max_concurrency_setting_is_honoured_for_parallel_stages():
    invoker = FakeSeatInvoker()
    invoker.delay_s = 0.02
    run(
        make_runner(full_stages(), invoker).run(
            request(settings=CouncilSettings(max_concurrency=1))
        )
    )
    assert invoker.max_in_flight == 1
    invoker2 = FakeSeatInvoker()
    invoker2.delay_s = 0.02
    run(
        make_runner(full_stages(), invoker2).run(
            request(settings=CouncilSettings(max_concurrency=5))
        )
    )
    assert invoker2.max_in_flight == 5


# ── profile selection ───────────────────────────────────────────────────────


def test_unknown_and_non_runnable_profiles_raise_before_anything_runs():
    invoker = FakeSeatInvoker()
    runner = make_runner(full_stages(), invoker)
    with pytest.raises(UnknownProfile):
        run(runner.run(request(profile_id="nope")))
    with pytest.raises(ProfileNotRunnable):
        run(runner.run(request(profile_id="coding")))
    assert invoker.calls == []


# ── failure semantics ───────────────────────────────────────────────────────


def test_below_quorum_fails_the_required_stage_and_skips_everything_after_it():
    invoker = FakeSeatInvoker()
    for seat in ("analyst", "skeptic", "creative"):
        invoker.script(seat, ID.PERSPECTIVES, SeatStatus.TIMEOUT)
    outcome = run(make_runner(full_stages(), invoker).run(request()))
    results = by_stage(outcome)
    assert results[ID.PERSPECTIVES].status is StageStatus.FAILED
    for later in (ID.REVIEW, ID.REVISION, ID.ARBITRATION, ID.SYNTHESIS):
        assert results[later].status is StageStatus.SKIPPED
        assert results[later].skipped_reason == "upstream_failed:perspectives"
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.answer is None
    assert "perspectives failed" in outcome.failure_summary
    assert not invoker.calls_for("synthesizer")
    assert not invoker.calls_for("arbitrator")


def test_missing_required_set_member_fails_even_when_count_is_met():
    invoker = FakeSeatInvoker()
    invoker.script("skeptic", ID.PERSPECTIVES, SeatStatus.PROVIDER_ERROR)
    invoker.script("fact_checker", ID.PERSPECTIVES, SeatStatus.EMPTY)
    outcome = run(make_runner(full_stages(), invoker).run(request()))
    assert by_stage(outcome)[ID.PERSPECTIVES].status is StageStatus.FAILED  # 3 ok, none of the set
    assert outcome.status is OutcomeStatus.FAILED and outcome.answer is None


def test_an_absent_seat_degrades_the_run_and_is_recorded_not_defaulted():
    invoker = FakeSeatInvoker()
    invoker.script("creative", ID.PERSPECTIVES, SeatStatus.UNPARSEABLE)
    outcome = run(make_runner(full_stages(), invoker).run(request()))
    perspectives = by_stage(outcome)[ID.PERSPECTIVES]
    assert perspectives.status is StageStatus.DEGRADED
    assert perspectives.degradations == ("seat_unavailable:creative",)
    assert "creative" not in {a.author for a in perspectives.artifacts}
    assert outcome.status is OutcomeStatus.DEGRADED
    assert outcome.answer == "synthesizer:synthesis"
    assert "seat_unavailable:creative" in outcome.degradations


def test_optional_stage_failure_degrades_but_does_not_fail_the_run():
    invoker = FakeSeatInvoker()
    for seat in ("analyst", "skeptic", "creative", "fact_checker", "practicalist"):
        invoker.script(seat, ID.REVIEW, SeatStatus.TIMEOUT)
    outcome = run(make_runner(full_stages(), invoker).run(request()))
    review = by_stage(outcome)[ID.REVIEW]
    assert review.status is StageStatus.DEGRADED
    assert "quorum_not_met" in review.degradations  # recorded, but an optional stage never fails
    assert outcome.status is OutcomeStatus.DEGRADED
    assert outcome.answer is not None
    assert by_stage(outcome)[ID.SYNTHESIS].status is StageStatus.COMPLETED


def test_failed_seat_calls_never_become_artifacts():
    invoker = FakeSeatInvoker()
    invoker.script("analyst", ID.PERSPECTIVES, SeatStatus.AUTH_ERROR)
    outcome = run(make_runner(full_stages(), invoker).run(request()))
    authors = {a.author for a in by_stage(outcome)[ID.PERSPECTIVES].artifacts}
    assert "analyst" not in authors and len(authors) == 4


def test_a_model_fallback_is_recorded_but_is_not_degradation():
    def fell_back(call: SeatCall) -> SeatResult[str]:
        return SeatResult(
            seat_id=call.seat_id,
            kind=call.kind,
            stage=call.stage,
            status=SeatStatus.OK,
            payload="from the fallback model",
            fallback_used=True,
            attempts=2,
        )

    invoker = FakeSeatInvoker()
    invoker.script("analyst", ID.PERSPECTIVES, fell_back)
    outcome = run(make_runner(full_stages(), invoker).run(request()))
    assert outcome.status is OutcomeStatus.COMPLETED
    seat = next(
        r for r in by_stage(outcome)[ID.PERSPECTIVES].seat_results if r.seat_id == "analyst"
    )
    assert seat.fallback_used is True


def test_stage_exception_becomes_a_failed_stage_not_a_crash():
    async def boom(ctx):
        raise RuntimeError("stage blew up")

    stages = full_stages(perspectives=SeatStage(ID.PERSPECTIVES, behave=boom))
    outcome = run(make_runner(stages).run(request()))
    stage = by_stage(outcome)[ID.PERSPECTIVES]
    assert stage.status is StageStatus.FAILED
    assert stage.degradations == ("stage_error:RuntimeError",)
    assert outcome.status is OutcomeStatus.FAILED and outcome.answer is None
    assert "stage blew up" not in outcome.failure_summary  # never leaks raw exception text


def test_optional_stage_exception_only_degrades():
    async def boom(ctx):
        raise ValueError("review blew up")

    stages = full_stages(review=SeatStage(ID.REVIEW, behave=boom))
    outcome = run(make_runner(stages).run(request()))
    assert by_stage(outcome)[ID.REVIEW].status is StageStatus.DEGRADED
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is not None


def test_stage_timeout_becomes_a_failed_stage():
    async def hang(ctx):
        await asyncio.sleep(30)
        return StageOutput()

    stages = full_stages(perspectives=SeatStage(ID.PERSPECTIVES, behave=hang))
    outcome = run(
        make_runner(stages).run(request(settings=CouncilSettings(wall_budget_s=0.6))), timeout=10
    )
    stage = by_stage(outcome)[ID.PERSPECTIVES]
    assert stage.status is StageStatus.FAILED and stage.degradations == ("stage_timeout",)
    assert outcome.status is OutcomeStatus.FAILED and outcome.answer is None


def test_exhausted_wall_budget_fails_required_and_degrades_optional_stages():
    clock = FakeClock()

    async def burn(ctx):
        clock.advance(100.0)
        return await SeatStage(ctx.contract.stage).run(ctx)

    stages = full_stages(frame=SeatStage(ID.FRAME, behave=burn))
    outcome = run(
        make_runner(stages, clock=clock).run(request(settings=CouncilSettings(wall_budget_s=50)))
    )
    results = by_stage(outcome)
    assert results[ID.FRAME].status is StageStatus.COMPLETED
    assert results[ID.PERSPECTIVES].status is StageStatus.FAILED
    assert results[ID.PERSPECTIVES].degradations == ("wall_budget_exhausted",)
    assert outcome.status is OutcomeStatus.FAILED


def test_unregistered_stage_is_reported_by_criticality():
    stages = full_stages()
    del stages[ID.REVIEW]
    del stages[ID.SYNTHESIS]
    outcome = run(make_runner(stages).run(request()))
    results = by_stage(outcome)
    assert results[ID.REVIEW].status is StageStatus.DEGRADED
    assert results[ID.REVIEW].degradations == ("stage_not_registered:review",)
    assert results[ID.SYNTHESIS].status is StageStatus.FAILED
    assert outcome.status is OutcomeStatus.FAILED and outcome.answer is None


def test_empty_stage_set_is_a_failure_not_a_fake_success():
    outcome = run(make_runner({}).run(request()))
    assert outcome.status is OutcomeStatus.FAILED
    assert outcome.answer is None and outcome.failure_summary


# ── contract enforcement ────────────────────────────────────────────────────


def _violating(post):
    return full_stages(perspectives=SeatStage(ID.PERSPECTIVES, post=post))


def _violation_of(post) -> str:
    outcome = run(make_runner(_violating(post)).run(request()))
    stage = by_stage(outcome)[ID.PERSPECTIVES]
    assert stage.status is StageStatus.FAILED
    assert stage.artifacts == () and stage.seat_results == ()
    return stage.degradations[0]


def test_wrong_artifact_kind_is_rejected():
    def post(out):
        art = out.artifacts[0].model_copy(update={"kind": ArtifactKind.DECISION})
        return out.model_copy(update={"artifacts": (art, *out.artifacts[1:])})

    assert "may not write decision" in _violation_of(post)


def test_wrong_seat_kind_is_rejected():
    def post(out):
        art = out.artifacts[0].model_copy(update={"author": "arbitrator"})
        return out.model_copy(update={"artifacts": (art, *out.artifacts[1:])})

    assert "may not author" in _violation_of(post)


def test_unknown_seat_and_duplicate_results_are_rejected():
    def unknown(out):
        r = out.seat_results[0].model_copy(update={"seat_id": "ghost_seat"})
        return out.model_copy(update={"seat_results": (r, *out.seat_results[1:])})

    def duplicate(out):
        return out.model_copy(update={"seat_results": (*out.seat_results, out.seat_results[0])})

    assert "unknown seat" in _violation_of(unknown)
    assert "duplicate result" in _violation_of(duplicate)


def test_result_for_the_wrong_stage_is_rejected():
    def post(out):
        r = out.seat_results[0].model_copy(update={"stage": ID.REVIEW})
        return out.model_copy(update={"seat_results": (r, *out.seat_results[1:])})

    assert "returned from perspectives" in _violation_of(post)


def test_an_artifact_for_a_seat_that_did_not_deliver_is_a_violation():
    """A failure must not be dressed up as a contribution."""

    def post(out):
        failed = SeatResult.failure(
            seat_id="analyst",
            kind=out.seat_results[0].kind,
            stage=ID.PERSPECTIVES,
            status=SeatStatus.TIMEOUT,
        )
        others = tuple(r for r in out.seat_results if r.seat_id != "analyst")
        phantom = StageArtifact(kind=ArtifactKind.PERSPECTIVE, author="analyst", payload="made up")
        return out.model_copy(
            update={"seat_results": (failed, *others), "artifacts": (*out.artifacts, phantom)}
        )

    assert "without a delivered seat result" in _violation_of(post)


def test_a_declared_deterministic_fallback_may_stand_in_and_degrades_the_run():
    def post(out):
        failed = SeatResult.failure(
            seat_id="analyst",
            kind=out.seat_results[0].kind,
            stage=ID.PERSPECTIVES,
            status=SeatStatus.TIMEOUT,
        )
        others = tuple(r for r in out.seat_results if r.seat_id != "analyst")
        stand_in = StageArtifact(kind=ArtifactKind.PERSPECTIVE, author="analyst", payload="default")
        return out.model_copy(
            update={
                "seat_results": (failed, *others),
                "artifacts": (*out.artifacts, stand_in),
                "deterministic_fallback_used": True,
            }
        )

    outcome = run(make_runner(_violating(post)).run(request()))
    assert outcome.status is OutcomeStatus.DEGRADED  # fallback used, never plain completed


def test_unknown_expected_seat_is_rejected():
    def post(out):
        return out.model_copy(update={"expected_seats": ("ghost_seat",)})

    assert "unknown expected seat" in _violation_of(post)


def test_artifact_outputs_become_inert_outcome_artifacts():
    art = Artifact(kind="text", content="a deliverable")

    def post(out):
        extra = StageArtifact(kind=ArtifactKind.ARTIFACT, author="synthesizer", payload=art)
        return out.model_copy(update={"artifacts": (*out.artifacts, extra)})

    stages = full_stages(synthesis=SeatStage(ID.SYNTHESIS, post=post))
    outcome = run(make_runner(stages).run(request()))
    assert outcome.artifacts == (art,)


def test_registration_must_match_the_declared_contract():
    wrong = STAGE_CONTRACTS[ID.REVIEW].model_copy(update={"parallel": False})
    with pytest.raises(ValueError):
        make_runner({ID.REVIEW: SeatStage(ID.REVIEW, contract=wrong)})
    with pytest.raises(ValueError):
        make_runner({ID.FRAME: SeatStage(ID.REVIEW)})
    with pytest.raises(ValueError):
        make_runner([SeatStage(ID.FRAME), SeatStage(ID.FRAME)])


def test_stage_context_exposes_no_provider_or_state_object():
    names = set(StageContext.__dataclass_fields__)
    assert not names & {"provider", "provider_registry", "model", "state", "store", "mapper"}


# ── cancellation ────────────────────────────────────────────────────────────


def test_cancellation_mid_stage_keeps_completed_results_and_leaves_no_tasks():
    async def scenario():
        gate = asyncio.Event()

        async def slow(ctx):
            gate.set()
            await asyncio.sleep(30)
            return StageOutput()

        stages = full_stages(perspectives=SeatStage(ID.PERSPECTIVES, behave=slow))
        task = asyncio.ensure_future(make_runner(stages).run(request()))
        await gate.wait()
        task.cancel()
        with pytest.raises(CouncilCancelled) as info:
            await task
        await asyncio.sleep(0)
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return info.value.outcome, leftovers

    outcome, leftovers = run(scenario())
    assert leftovers == []
    assert outcome.status is OutcomeStatus.CANCELLED
    assert outcome.answer is None
    assert outcome.failure_summary == "cancelled during perspectives"
    assert [r.stage for r in outcome.stage_results] == [ID.FRAME]  # completed evidence retained
    assert outcome.stage_results[0].status is StageStatus.COMPLETED


def test_cancelled_error_is_still_a_cancelled_error():
    assert issubclass(CouncilCancelled, asyncio.CancelledError)


def test_cancellation_inside_parallel_seat_calls_cancels_every_pending_call():
    async def scenario():
        invoker = FakeSeatInvoker()
        invoker.delay_s = 30
        task = asyncio.ensure_future(make_runner(full_stages(), invoker).run(request()))
        while invoker.in_flight < 1:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(CouncilCancelled):
            await task
        await asyncio.sleep(0)
        return invoker.in_flight, [
            t for t in asyncio.all_tasks() if t is not asyncio.current_task()
        ]

    in_flight, leftovers = run(scenario())
    assert in_flight == 0 and leftovers == []


# ── determinism ─────────────────────────────────────────────────────────────


def test_trace_is_a_deterministic_ordered_sequence():
    events: list = []

    class Sink:
        def emit(self, event):
            events.append(event)

    outcome = run(make_runner(full_stages(), trace_sink=Sink()).run(request(depth=Depth.QUICK)))
    kinds = [(e.event, e.stage, e.seat) for e in events]
    assert kinds[0][0] is TraceEventKind.RUN_STARTED
    assert kinds[-1][0] is TraceEventKind.RUN_FINISHED
    assert [e.seq for e in events] == list(range(len(events)))
    started = [e.stage for e in events if e.event is TraceEventKind.STAGE_STARTED]
    assert started == [ID.FRAME, ID.PERSPECTIVES, ID.ARBITRATION, ID.SYNTHESIS]
    finished = [e for e in events if e.event is TraceEventKind.STAGE_FINISHED]
    assert len(finished) == 4
    seat_events = [e.seat for e in events if e.stage is ID.PERSPECTIVES and e.seat]
    assert seat_events == ["analyst", "skeptic", "creative", "fact_checker", "practicalist"]
    assert outcome.trace_digest


def test_same_run_twice_gives_identical_outcomes_and_digests():
    from velune.council.serialization import canonical_json

    first = run(make_runner(full_stages()).run(request()))
    second = run(make_runner(full_stages()).run(request()))
    assert canonical_json(first) == canonical_json(second)
    assert first.trace_digest == second.trace_digest


def test_trace_digest_changes_when_the_run_changes():
    clean = run(make_runner(full_stages()).run(request()))
    invoker = FakeSeatInvoker()
    invoker.script("creative", ID.PERSPECTIVES, SeatStatus.TIMEOUT)
    degraded = run(make_runner(full_stages(), invoker).run(request()))
    assert clean.trace_digest != degraded.trace_digest


def test_broken_trace_sink_does_not_break_the_run():
    class Broken:
        def emit(self, event):
            raise RuntimeError("sink down")

    outcome = run(make_runner(full_stages(), trace_sink=Broken()).run(request()))
    assert outcome.status is OutcomeStatus.COMPLETED


def test_runner_uses_injected_ids_not_uuid():
    ids = CounterIds()
    runner = StagedCouncilRunner(
        registry=default_registry(),
        stages=full_stages(),
        invoker=FakeSeatInvoker(),
        clock=FakeClock(),
        ids=ids,
    )
    run(runner.run(request()))
    run(runner.run(request()))
    assert ids.counts["run"] == 2
