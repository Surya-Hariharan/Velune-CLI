"""R0: the Moderator's neutral frame, its fallback, repair and cancellation."""

from __future__ import annotations

import asyncio

import pytest

from tests.council_core_fakes import CounterIds, FakeClock, FakeSeatInvoker
from tests.council_scripted import (
    StaticPrompts,
    make_request,
    valid_frame_json,
)
from velune.cognition.execution_trace import CallReason
from velune.council.contracts import Frame
from velune.council.domain import StageId
from velune.council.frame import FrameStage
from velune.council.profiles import default_registry
from velune.council.report import frame_of
from velune.council.request import EvidenceItem, ResponseRequirements
from velune.council.results import OutcomeStatus, SeatStatus, StageStatus
from velune.council.runner import CouncilCancelled, StagedCouncilRunner
from velune.council.serialization import canonical_json
from velune.council.trace import TraceEventKind

FRAME = StageId.FRAME
NON_OK = [s for s in SeatStatus if s is not SeatStatus.OK]


class Events:
    def __init__(self) -> None:
        self.items = []

    def emit(self, event) -> None:
        self.items.append(event)

    def kinds(self):
        return [e.event for e in self.items]


def make_runner(invoker, events=None) -> StagedCouncilRunner:
    return StagedCouncilRunner(
        registry=default_registry(),
        stages=[FrameStage(StaticPrompts())],
        invoker=invoker,
        clock=FakeClock(),
        ids=CounterIds(),
        trace_sink=events,
    )


def run(invoker, request=None, events=None):
    runner = make_runner(invoker, events)
    return asyncio.run(
        asyncio.wait_for(runner.run(request or make_request(), plan=(FRAME,)), timeout=15)
    )


def frame_of_outcome(outcome, request=None) -> Frame:
    return frame_of(outcome, request or make_request())


def scripted(*entries) -> FakeSeatInvoker:
    invoker = FakeSeatInvoker()
    invoker.script("moderator", FRAME, *entries)
    return invoker


def test_healthy_run_produces_a_typed_neutral_frame_and_no_answer():
    invoker = scripted(valid_frame_json())
    outcome = run(invoker)
    stage = outcome.stage_results[0]
    frame = frame_of_outcome(outcome)
    assert isinstance(frame, Frame) and frame.degraded is False
    assert frame.problem_type == "decision" and frame.dimensions == ("cost", "operability", "scale")
    assert stage.status is StageStatus.COMPLETED and stage.degradations == ()
    assert [(a.kind.value, a.author) for a in stage.artifacts] == [("frame", "moderator")]
    assert outcome.status is OutcomeStatus.COMPLETED
    assert outcome.answer is None  # R0 alone is evidence, never an answer
    assert len(invoker.calls) == 1


def test_only_the_moderator_is_called_and_it_sees_no_evidence_or_other_seat():
    request = make_request(
        context="small team",
        evidence=(EvidenceItem(id="e1", text="EVIDENCE-TOKEN"),),
        response=ResponseRequirements(language="en"),
    )
    invoker = scripted(valid_frame_json())
    run(invoker, request)
    (call,) = invoker.calls
    assert call.seat_id == "moderator" and call.stage is FRAME
    assert call.reason is CallReason.PRIMARY
    body = "\n".join(m.content for m in call.messages)
    assert "EVIDENCE-TOKEN" not in body
    for seat in ("analyst", "skeptic", "creative", "fact_checker", "practicalist"):
        assert f'<seat id="{seat}"' not in body
    assert "small team" in body


def test_language_comes_from_the_request_not_the_model():
    request = make_request(response=ResponseRequirements(language="de"))
    frame = frame_of_outcome(run(scripted(valid_frame_json()), request))
    assert frame.language == "de"


def test_the_moderator_cannot_set_degraded_or_language():
    invoker = scripted(valid_frame_json(degraded=True), valid_frame_json())
    outcome = run(invoker)
    assert frame_of_outcome(outcome).degraded is False
    assert len(invoker.calls) == 2  # the forbidden field cost one repair


def test_needs_clarification_is_carried_and_never_acted_on():
    invoker = scripted(
        valid_frame_json(needs_clarification=True, clarification_question="Which region?")
    )
    outcome = run(invoker)
    frame = frame_of_outcome(outcome)
    assert frame.needs_clarification is True and frame.clarification_question == "Which region?"
    assert len(invoker.calls) == 1 and outcome.status is OutcomeStatus.COMPLETED


def test_fenced_json_is_accepted():
    outcome = run(scripted("```json\n" + valid_frame_json() + "\n```"))
    assert frame_of_outcome(outcome).problem_type == "decision"


# ── failure: the Moderator is an absence, R0 never blocks the run ───────────


@pytest.mark.parametrize("status", NON_OK)
def test_any_moderator_failure_yields_the_deterministic_frame_and_a_degraded_run(status):
    events = Events()
    outcome = run(scripted(status), events=events)
    stage = outcome.stage_results[0]
    result = stage.seat_results[0]
    # the absence is recorded, with no payload dressed up as the seat's frame
    assert result.status is status and result.payload is None
    assert stage.status is StageStatus.DEGRADED
    assert "seat_unavailable:moderator" in stage.degradations
    assert [a.kind.value for a in stage.artifacts] == ["frame"]
    assert outcome.status is OutcomeStatus.DEGRADED and outcome.answer is None
    assert TraceEventKind.FRAME_FALLBACK in events.kinds()


def test_the_fallback_frame_is_the_question_only_and_marked_degraded():
    request = make_request(question="What is the airspeed of a swallow?")
    outcome = run(scripted(SeatStatus.TIMEOUT), request)
    frame = frame_of(outcome, request)
    assert frame.degraded is True and frame.problem_type == "other"
    assert frame.question_restated == "What is the airspeed of a swallow?"
    assert not (frame.constraints or frame.dimensions or frame.ambiguities or frame.language)
    # nothing the failed Moderator might have said survives into the frame
    assert outcome.stage_results[0].seat_results[0].payload is None


# ── malformed output: exactly one repair ────────────────────────────────────


def test_malformed_then_valid_is_repaired_once_with_only_the_seats_own_reply():
    events = Events()
    invoker = scripted("here you go: not json", valid_frame_json())
    outcome = run(invoker, events=events)
    first, second = invoker.calls
    assert first.reason is CallReason.PRIMARY and second.reason is CallReason.VALIDATION_FAILURE
    assert [m.role for m in second.messages] == ["system", "user", "assistant", "user"]
    assert second.messages[:2] == first.messages
    assert second.messages[2].content == "here you go: not json"
    assert "rejected" in second.messages[3].content
    result = outcome.stage_results[0].seat_results[0]
    assert result.ok and result.attempts == 2
    assert outcome.stage_results[0].status is StageStatus.COMPLETED
    assert TraceEventKind.SEAT_REPAIR in events.kinds()
    assert TraceEventKind.FRAME_FALLBACK not in events.kinds()


def test_malformed_twice_is_unparseable_and_falls_back():
    events = Events()
    invoker = scripted("nope", "still nope")
    outcome = run(invoker, events=events)
    assert len(invoker.calls) == 2  # exactly one repair, never a loop
    result = outcome.stage_results[0].seat_results[0]
    assert result.status is SeatStatus.UNPARSEABLE and result.payload is None
    assert result.attempts == 2
    assert outcome.status is OutcomeStatus.DEGRADED
    assert TraceEventKind.FRAME_FALLBACK in events.kinds()


def test_a_problem_type_outside_the_vocabulary_is_a_validation_failure():
    invoker = scripted(valid_frame_json(problem_type="astrology"), valid_frame_json())
    outcome = run(invoker)
    assert frame_of_outcome(outcome).problem_type == "decision" and len(invoker.calls) == 2
    assert "problem_type" in invoker.calls[1].messages[3].content


@pytest.mark.parametrize("hidden", ["reasoning", "thoughts", "scratchpad"])
def test_private_reasoning_fields_are_never_accepted(hidden):
    invoker = scripted(valid_frame_json(**{hidden: "my hidden notes"}), valid_frame_json())
    outcome = run(invoker)
    frame = frame_of_outcome(outcome)
    assert hidden not in canonical_json(frame) and "hidden notes" not in canonical_json(frame)
    assert "hidden notes" not in invoker.calls[1].messages[3].content  # not echoed back either


def test_a_repair_that_fails_at_the_provider_keeps_its_typed_status():
    invoker = scripted("garbage", SeatStatus.TIMEOUT)
    result = run(invoker).stage_results[0].seat_results[0]
    assert result.status is SeatStatus.TIMEOUT and result.payload is None and result.attempts == 2


def test_a_stage_level_bug_degrades_r0_without_inventing_a_frame():
    invoker = scripted(RuntimeError("bug in the invoker"))
    outcome = run(invoker)
    stage = outcome.stage_results[0]
    assert stage.status is StageStatus.DEGRADED
    assert stage.degradations == ("stage_error:RuntimeError",)
    assert stage.artifacts == () and outcome.answer is None


# ── cancellation and determinism ────────────────────────────────────────────


def test_cancellation_mid_frame_stops_cleanly_and_carries_a_cancelled_outcome():
    async def scenario():
        invoker = scripted(valid_frame_json())
        invoker.delay_s = 30
        task = asyncio.ensure_future(make_runner(invoker).run(make_request(), plan=(FRAME,)))
        while invoker.in_flight < 1:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(CouncilCancelled) as info:
            await task
        await asyncio.sleep(0)
        leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        return info.value.outcome, invoker.in_flight, leftovers

    outcome, in_flight, leftovers = asyncio.run(asyncio.wait_for(scenario(), timeout=15))
    assert outcome.status is OutcomeStatus.CANCELLED and outcome.answer is None
    assert outcome.failure_summary == "cancelled during frame"
    assert in_flight == 0 and leftovers == []


def test_the_same_run_twice_is_byte_identical():
    first = run(scripted(valid_frame_json()))
    second = run(scripted(valid_frame_json()))
    assert canonical_json(first) == canonical_json(second)
    assert first.trace_digest == second.trace_digest


def test_trace_events_are_ordered_and_hold_no_text():
    events = Events()
    run(scripted("bad", valid_frame_json()), events=events)
    kinds = events.kinds()
    assert kinds[0] is TraceEventKind.RUN_STARTED and kinds[-1] is TraceEventKind.RUN_FINISHED
    assert kinds.index(TraceEventKind.SEAT_REPAIR) < kinds.index(TraceEventKind.SEAT_RESULT)
    assert "plan=frame" in events.items[0].detail
    for event in events.items:
        assert "bad" not in event.detail and "decision" not in event.detail
