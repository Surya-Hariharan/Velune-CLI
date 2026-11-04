"""The reasoning runner: outcomes for success and every failure, and cancellation that propagates."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from tests.council_core_fakes import CounterIds, FakeClock
from tests.engine_fakes import ExplodingTrace, FakeReasoningInvoker, TraceLog
from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind, ReadScope
from velune.council.profiles import RoutingRole
from velune.council.results import ModelRef, SeatStatus
from velune.council.serialization import digest
from velune.council.trace import TraceEventKind
from velune.orchestration.engine import (
    AgentBudget,
    AgentFailure,
    AgentInput,
    AgentKind,
    AgentOutcome,
    AgentRunner,
    AgentSpec,
    ContextItem,
    FailureKind,
    OutcomeStatus,
    ReadGrant,
    ReasoningAgentRunner,
    ReasoningInvoker,
    ReasoningResult,
    StaticPromptResolver,
    ToolPolicy,
    advance,
    cancel,
)
from velune.orchestration.state import RunPhase, RunState, RunStatus

PROMPTS = StaticPromptResolver({"engine.planner": "You are the planner.", "engine.blank": "  "})


def spec(**kw) -> AgentSpec:
    kw.setdefault("role", AgentRole.PLANNER)
    kw.setdefault("routing_role", RoutingRole.PLANNER)
    kw.setdefault("prompt_ref", "engine.planner")
    kw.setdefault(
        "read_scope",
        (
            ReadGrant(kind=ArtifactKind.QUESTION, scope=ReadScope.ALL),
            ReadGrant(kind=ArtifactKind.CRITIQUE, scope=ReadScope.OWN),
        ),
    )
    return AgentSpec(**kw)


def task(**kw) -> AgentInput:
    kw.setdefault("run_id", "run-1")
    kw.setdefault("instruction", "Plan the change.")
    return AgentInput(**kw)


def make(*script, delay_s=0.0, trace=None, screen=None, grace_s=0.0, clock=None):
    invoker = FakeReasoningInvoker(*script, delay_s=delay_s)
    clock = clock or FakeClock()
    runner = ReasoningAgentRunner(
        invoker, PROMPTS, clock=clock, ids=CounterIds(), trace=trace, screen=screen, grace_s=grace_s
    )
    return runner, invoker, clock


def failure_kind(outcome: AgentOutcome) -> FailureKind:
    assert outcome.failure is not None
    return outcome.failure.kind


# ── success ────────────────────────────────────────────────────────────────


async def test_success_returns_a_typed_ok_outcome():
    runner, invoker, _ = make("  the plan  ")
    outcome = await runner.run(spec(), task())
    assert outcome.ok and outcome.status is OutcomeStatus.OK
    assert (outcome.role, outcome.text, outcome.failure) == (AgentRole.PLANNER, "the plan", None)
    assert outcome.call_id == "call-1"
    assert len(invoker.requests) == 1


async def test_the_request_names_a_routing_role_and_carries_the_budget():
    runner, invoker, _ = make("ok")
    budget = AgentBudget(timeout_s=7, max_output_tokens=333, max_input_chars=5000)
    await runner.run(spec(budget=budget), task(attempt=2))
    request = invoker.requests[0]
    assert request.routing_role is RoutingRole.PLANNER and request.agent is AgentRole.PLANNER
    assert (request.timeout_s, request.max_tokens, request.attempt) == (7, 333, 2)
    assert request.run_id == "run-1" and request.call_id == "call-1"
    assert [m.role for m in request.messages] == ["system", "user"]
    assert request.messages[0].content == "You are the planner."
    assert request.messages[1].content == "Plan the change."
    assert (
        "provider" not in type(request).model_fields and "model" not in type(request).model_fields
    )


async def test_context_is_rendered_with_its_kind_and_author():
    runner, invoker, _ = make("ok")
    context = (
        ContextItem(kind=ArtifactKind.QUESTION, text="why?"),
        ContextItem(kind=ArtifactKind.CRITIQUE, text="too big", author=AgentRole.PLANNER),
    )
    await runner.run(spec(), task(context=context))
    user = invoker.requests[0].messages[1].content
    assert user == "Plan the change.\n\n[question]\nwhy?\n\n[critique from planner]\ntoo big"


async def test_the_model_identity_is_passed_through_and_timing_uses_the_injected_clock():
    clock = FakeClock()
    model = ModelRef(provider_id="p", model_id="m")

    def slow(_request):
        clock.advance(2.5)
        return ReasoningResult.success("done", model=model)

    runner, _, _ = make(slow, clock=clock)
    outcome = await runner.run(spec(), task())
    assert outcome.model == model and outcome.elapsed_ms == 2500


async def test_calls_get_distinct_ids_and_the_input_is_not_mutated():
    runner, _, _ = make("a", "b")
    request = task()
    first = await runner.run(spec(), request)
    second = await runner.run(spec(), request)
    assert (first.call_id, second.call_id) == ("call-1", "call-2")
    assert request == task()


async def test_identical_runs_produce_identical_digests():
    one = await make("same")[0].run(spec(), task())
    two = await make("same")[0].run(spec(), task())
    assert digest(one) == digest(two)  # elapsed_ms is volatile and excluded


# ── failures become explicit outcomes ──────────────────────────────────────


@pytest.mark.parametrize(
    "status, kind",
    [
        (SeatStatus.TIMEOUT, FailureKind.TIMEOUT),
        (SeatStatus.PROVIDER_ERROR, FailureKind.PROVIDER_ERROR),
        (SeatStatus.AUTH_ERROR, FailureKind.AUTH_ERROR),
        (SeatStatus.EMPTY, FailureKind.EMPTY),
        (SeatStatus.UNPARSEABLE, FailureKind.UNPARSEABLE),
        (SeatStatus.BLOCKED, FailureKind.BLOCKED),
        (SeatStatus.SKIPPED, FailureKind.UNSUPPORTED),
    ],
)
async def test_every_invoker_failure_becomes_a_failed_outcome(status, kind):
    runner, _, _ = make(status)
    outcome = await runner.run(spec(), task())
    assert not outcome.ok and outcome.status is OutcomeStatus.FAILED
    assert failure_kind(outcome) is kind and outcome.text is None


async def test_whitespace_only_text_is_an_empty_failure_not_success():
    outcome = await make("   \n ")[0].run(spec(), task())
    assert failure_kind(outcome) is FailureKind.EMPTY and outcome.text is None


async def test_an_invoker_that_breaks_its_contract_is_a_failure_not_a_crash():
    runner, _, _ = make(RuntimeError("secret-token-123 leaked"))
    outcome = await runner.run(spec(), task())
    assert failure_kind(outcome) is FailureKind.INTERNAL
    assert "RuntimeError" in outcome.failure.message
    assert "secret-token-123" not in outcome.model_dump_json()


async def test_an_invoker_that_hangs_times_out_as_a_failure():
    runner, _, _ = make("late", delay_s=5, grace_s=0)
    outcome = await runner.run(spec(budget=AgentBudget(timeout_s=0.05)), task())
    assert failure_kind(outcome) is FailureKind.TIMEOUT


async def test_the_invokers_own_timeout_result_wins_inside_the_grace_period():
    runner, _, _ = make(SeatStatus.TIMEOUT, grace_s=1.0)
    outcome = await runner.run(spec(), task())
    assert failure_kind(outcome) is FailureKind.TIMEOUT


async def test_an_acting_agent_is_refused_without_calling_the_model():
    acting = spec(
        role=AgentRole.CODER,
        routing_role=RoutingRole.CODER,
        kind=AgentKind.ACTING,
        tool_policy=ToolPolicy(allowed_tools=("write_file",)),
    )
    runner, invoker, _ = make("never")
    outcome = await runner.run(acting, task())
    assert failure_kind(outcome) is FailureKind.UNSUPPORTED
    assert "acting agents cannot run" in outcome.failure.message
    assert invoker.requests == []


async def test_context_outside_the_read_scope_is_refused_without_calling_the_model():
    runner, invoker, _ = make("never")
    outcome = await runner.run(
        spec(), task(context=(ContextItem(kind=ArtifactKind.EVIDENCE, text="secret"),))
    )
    assert failure_kind(outcome) is FailureKind.SCOPE_VIOLATION
    assert "may not read evidence" in outcome.failure.message
    assert invoker.requests == []


async def test_own_scope_admits_only_the_agents_own_artifacts():
    runner, invoker, _ = make("ok")
    mine = ContextItem(kind=ArtifactKind.CRITIQUE, text="mine", author=AgentRole.PLANNER)
    theirs = ContextItem(kind=ArtifactKind.CRITIQUE, text="theirs", author=AgentRole.REVIEWER)
    anonymous = ContextItem(kind=ArtifactKind.CRITIQUE, text="who?")
    assert (await runner.run(spec(), task(context=(mine,)))).ok
    for item in (theirs, anonymous):
        outcome = await runner.run(spec(), task(context=(item,)))
        assert failure_kind(outcome) is FailureKind.SCOPE_VIOLATION
    assert len(invoker.requests) == 1


async def test_an_unknown_or_blank_prompt_is_invalid_input():
    runner, invoker, _ = make("never")
    missing = await runner.run(spec(prompt_ref="engine.missing"), task())
    blank = await runner.run(spec(prompt_ref="engine.blank"), task())
    assert (
        failure_kind(missing) is FailureKind.INVALID_INPUT
        and "engine.missing" in missing.failure.message
    )
    assert failure_kind(blank) is FailureKind.INVALID_INPUT
    assert invoker.requests == []


async def test_input_over_the_budget_is_refused():
    runner, invoker, _ = make("never")
    small = spec(budget=AgentBudget(max_input_chars=30))
    outcome = await runner.run(small, task(instruction="x" * 100))
    assert (
        failure_kind(outcome) is FailureKind.INVALID_INPUT and "budget" in outcome.failure.message
    )
    assert invoker.requests == []


async def test_the_content_screen_can_block_input_before_the_model_sees_it():
    class Deny:
        seen: list[str] = []

        def allows(self, text: str) -> bool:
            self.seen.append(text)
            return False

    screen = Deny()
    runner, invoker, _ = make("never", screen=screen)
    outcome = await runner.run(spec(), task())
    assert failure_kind(outcome) is FailureKind.BLOCKED
    assert invoker.requests == [] and screen.seen == ["Plan the change."]


async def test_a_programming_error_in_a_collaborator_is_not_swallowed():
    class Unknown:
        def resolve(self, ref: str) -> str:
            raise KeyError(ref)  # a LookupError: an unknown prompt, reported as invalid input

    class Bug:
        def resolve(self, ref: str) -> str:
            raise TypeError("a real bug")

    runner = ReasoningAgentRunner(
        FakeReasoningInvoker(), Bug(), clock=FakeClock(), ids=CounterIds()
    )
    with pytest.raises(TypeError, match="a real bug"):
        await runner.run(spec(), task())
    runner = ReasoningAgentRunner(
        FakeReasoningInvoker(), Unknown(), clock=FakeClock(), ids=CounterIds()
    )
    assert failure_kind(await runner.run(spec(), task())) is FailureKind.INVALID_INPUT


# ── cancellation ───────────────────────────────────────────────────────────


async def test_cancelling_the_task_stops_the_run_and_nothing_is_left_running():
    runner, invoker, _ = make("late", delay_s=30, grace_s=60)
    running = asyncio.ensure_future(runner.run(spec(), task()))
    for _ in range(5):
        await asyncio.sleep(0)
    assert invoker.requests, "the call should be in flight"
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert running.cancelled()


async def test_an_invoker_that_raises_cancelled_error_propagates_and_is_traced():
    trace = TraceLog()
    runner, _, _ = make(asyncio.CancelledError(), trace=trace)
    with pytest.raises(asyncio.CancelledError):
        await runner.run(spec(), task())
    assert [e.event for e in trace.events] == [TraceEventKind.CANCELLED]
    assert trace.events[0].seat == "planner"


async def test_a_cancelled_status_result_is_cancellation_never_a_retryable_failure():
    runner, _, _ = make(SeatStatus.CANCELLED)
    with pytest.raises(asyncio.CancelledError):
        await runner.run(spec(), task())


async def test_cancellation_mid_run_is_recorded_by_the_state_machine():
    state = advance(RunState.start("run-1"), RunPhase.EXECUTE)
    runner, _, _ = make(asyncio.CancelledError())
    try:
        await runner.run(spec(), task())
    except asyncio.CancelledError:
        state = cancel(state)
    assert state.phase is RunPhase.CANCELLED and state.status is RunStatus.CANCELLED


# ── tracing ────────────────────────────────────────────────────────────────


async def test_each_run_emits_one_ordered_event_with_no_prompt_or_completion_text():
    trace = TraceLog()
    runner, _, _ = make("a secret completion", SeatStatus.PROVIDER_ERROR, trace=trace)
    await runner.run(spec(), task(instruction="a secret instruction"))
    await runner.run(spec(), task())
    assert [(e.seq, e.event, e.status) for e in trace.events] == [
        (0, TraceEventKind.SEAT_RESULT, "ok"),
        (1, TraceEventKind.SEAT_RESULT, "provider_error"),
    ]
    assert all(e.stage is None and e.seat == "planner" for e in trace.events)
    dump = " ".join(e.model_dump_json() for e in trace.events)
    assert "secret" not in dump


async def test_a_broken_trace_sink_never_breaks_a_run():
    runner, _, _ = make("fine", "fine", trace=ExplodingTrace())
    assert (await runner.run(spec(), task())).ok
    assert (await runner.run(spec(), task())).ok


# ── contracts ──────────────────────────────────────────────────────────────


def test_the_runner_and_the_fake_satisfy_the_protocols():
    runner, invoker, _ = make()
    assert isinstance(runner, AgentRunner) and isinstance(invoker, ReasoningInvoker)


def test_an_outcome_cannot_look_like_both_success_and_failure():
    failure = AgentFailure(kind=FailureKind.TIMEOUT)
    base = {"role": AgentRole.PLANNER, "call_id": "c-1"}
    with pytest.raises(ValidationError):
        AgentOutcome(**base, status=OutcomeStatus.OK)
    with pytest.raises(ValidationError):
        AgentOutcome(**base, status=OutcomeStatus.OK, text="x", failure=failure)
    with pytest.raises(ValidationError):
        AgentOutcome(**base, status=OutcomeStatus.FAILED, text="x", failure=failure)
    with pytest.raises(ValidationError):
        AgentOutcome(**base, status=OutcomeStatus.FAILED)


def test_a_reasoning_result_cannot_look_like_both_success_and_failure():
    with pytest.raises(ValidationError):
        ReasoningResult(status=SeatStatus.OK)
    with pytest.raises(ValidationError):
        ReasoningResult(status=SeatStatus.TIMEOUT, text="x")
    with pytest.raises(ValueError):
        ReasoningResult.failure(SeatStatus.OK)


async def test_outcomes_round_trip_through_json():
    runner, _, _ = make("hello", SeatStatus.AUTH_ERROR)
    for _ in range(2):
        outcome = await runner.run(spec(), task())
        assert AgentOutcome.model_validate_json(outcome.model_dump_json()) == outcome
