"""Phase 4E-8 Stage B: one run control, one budget and one deadline across plan, execute and review.

Offline only: scripted fake providers, synthetic canary secrets, no network, no CLI, no credentials.
"""

from __future__ import annotations

import ast
import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from tests.test_engine_acting_reliability import CANARY
from tests.test_engine_cli_integration import (
    OFF,
    ON,
    Providers,
    asker,
)
from tests.test_engine_provider_integration import ScriptedProvider, read_call, reply
from velune.core.errors.provider import InferenceError
from velune.orchestration.engine.driver import DriverConfigError, PlanReviewDriver
from velune.orchestration.engine.errors import EngineDisabledError
from velune.orchestration.engine.execcontrol import ExecutionBudget, RunControl
from velune.orchestration.engine.limits import ControlLimits
from velune.orchestration.engine.ports import ReasoningRequest, SeatStatus
from velune.orchestration.engine_adapters import native_session
from velune.orchestration.engine_adapters.native_session import (
    NativeSessionError,
    run_native_plan_review,
)
from velune.orchestration.engine_adapters.provider_acting import ProviderRetryPolicy
from velune.orchestration.engine_adapters.reasoning_invoker import (
    BUDGET_SPENT,
    DEADLINE_REACHED,
    ProviderReasoningInvoker,
)
from velune.orchestration.engine_adapters.secrets import SecretScrubber, static_source

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("alpha contents" + chr(10), encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta contents" + chr(10), encoding="utf-8")
    return tmp_path


APPROVE = "VERDICT: APPROVE\nLooks right."
REVISE = "VERDICT: REVISE\nThe draft misses the file name."


def good_run(*, review: str = APPROVE) -> ScriptedProvider:
    """plan -> (execute: one read, then the draft) -> review"""
    return ScriptedProvider(
        reply("1. read a.txt\n2. summarize it"),
        reply("", [read_call()]),
        reply("It says alpha."),
        reply(review),
    )


async def go_pr(workspace: Path, provider: Any, ask: Any, *, config: Any = ON, **kw: Any):
    providers = kw.pop("providers", None) or Providers(provider)
    return await run_native_plan_review(
        "Read a.txt and tell me what it says.",
        config=config,
        providers=providers,
        provider_id=kw.pop("provider_id", "scripted"),
        model_id=kw.pop("model_id", "some-model"),
        workspace=workspace,
        ask=ask,
        **kw,
    )


def reasoning_requests(provider: ScriptedProvider) -> list[Any]:
    return [r for r in provider.requests if not r.tools]


def acting_requests(provider: ScriptedProvider) -> list[Any]:
    return [r for r in provider.requests if r.tools]


class Timed:
    """A fake provider whose i-th reply arrives after ``delays[i]`` seconds (last repeats)."""

    provider_id = "timed"

    def __init__(self, *steps: tuple[float, Any]) -> None:
        self.steps = list(steps)
        self.requests: list[Any] = []
        self.times: list[float] = []

    async def infer(self, request: Any) -> Any:
        self.requests.append(request)
        self.times.append(time.monotonic())
        delay, entry = self.steps[0]
        if len(self.steps) > 1:
            self.steps.pop(0)
        await asyncio.sleep(delay)
        if isinstance(entry, BaseException):
            raise entry
        return entry


# ── the flag and the explicit provider ────────────────────────────────────────


async def test_a_disabled_flag_builds_and_calls_nothing(workspace):
    providers = Providers(good_run())
    with pytest.raises(EngineDisabledError):
        await go_pr(workspace, None, asker(True), config=OFF, providers=providers)
    assert providers.lookups == [] and providers.provider.requests == []


async def test_an_unavailable_provider_is_an_error_and_is_looked_up_once(workspace):
    providers = Providers(None)
    with pytest.raises(NativeSessionError):
        await go_pr(workspace, None, asker(True), providers=providers, provider_id="groq")
    assert providers.lookups == ["groq"]


@pytest.mark.parametrize("task", ["", "  ", "x" * 5000])
async def test_an_unusable_task_is_refused_before_any_lookup(workspace, task):
    providers = Providers(good_run())
    with pytest.raises(NativeSessionError):
        await run_native_plan_review(
            task,
            config=ON,
            providers=providers,
            provider_id="p",
            model_id="m",
            workspace=workspace,
            ask=asker(True),
        )
    assert providers.lookups == []


# ── a complete run: plan, execute, review ─────────────────────────────────────


async def test_a_complete_run_plans_executes_with_one_approved_read_and_reviews(workspace):
    provider = good_run()
    result = await go_pr(workspace, provider, asker(True))
    assert result.ok and result.status == "completed" and result.answer == "It says alpha."
    assert result.message == "" and result.failure_kind is None
    assert len(provider.requests) == 4
    # accounting: one authoritative ledger saw planner, both executor turns and the reviewer
    assert (result.model_calls, result.provider_attempts, result.tool_calls) == (4, 4, 1)
    assert result.review_rounds == 1 and result.same_model_review is True
    assert (result.provider_id, result.model_id) == ("scripted", "some-model")


async def test_the_planner_and_reviewer_get_no_tools_and_only_the_executor_gets_read_file(
    workspace,
):
    provider = good_run()
    await go_pr(workspace, provider, asker(True))
    assert len(reasoning_requests(provider)) == 2 and len(acting_requests(provider)) == 2
    for request in acting_requests(provider):
        names = [t["function"]["name"] for t in request.tools]
        assert names == ["read_file"]
    assert all(r.tool_choice is None for r in reasoning_requests(provider))


async def test_every_request_uses_the_one_chosen_model_and_the_registry_is_asked_once(workspace):
    providers = Providers(good_run())
    await go_pr(workspace, None, asker(True), providers=providers, model_id="the-model")
    assert providers.lookups == ["scripted"]
    assert {r.model_id for r in providers.provider.requests} == {"the-model"}


async def test_the_reviewer_sees_the_draft_and_the_plan_but_not_the_tool_output(workspace):
    provider = good_run()
    await go_pr(workspace, provider, asker(True))
    review = reasoning_requests(provider)[1]
    text = str(review.messages)
    assert "It says alpha." in text and "1. read a.txt" in text
    assert "alpha contents" not in text  # the file body is tool evidence; the reviewer has no grant
    execute_turn_two = acting_requests(provider)[1]
    assert "alpha contents" in str(execute_turn_two.messages)


async def test_a_revision_replaces_the_draft_and_the_final_answer_is_the_latest_approved_one(
    workspace,
):
    provider = ScriptedProvider(
        reply("1. read a.txt"),
        reply("", [read_call()]),
        reply("first draft"),
        reply(REVISE),
        reply("", [read_call("a.txt", "call_2")]),
        reply("second draft, revised"),
        reply(APPROVE),
    )

    async def yes(prompt: Any) -> bool:
        return True

    result = await go_pr(workspace, provider, yes)
    assert result.ok and result.answer == "second draft, revised"
    assert result.review_rounds == 2
    revision_prompt = str(acting_requests(provider)[2].messages)
    assert "The draft misses the file name." in revision_prompt  # the critique reached the executor
    assert "first draft" in revision_prompt


async def test_a_review_that_never_approves_ends_at_the_revision_limit(workspace):
    provider = ScriptedProvider(
        reply("1. plan"), reply("a draft"), reply(REVISE), reply("another draft"), reply(REVISE)
    )
    result = await go_pr(workspace, provider, asker(True))
    assert not result.ok and result.failure_kind == "retry_limit" and result.status == "failed"
    assert result.answer is None
    assert (
        len(provider.requests) == 5
    )  # plan, draft, review, draft, review: nothing after the limit


# ── one RunControl for the whole run ──────────────────────────────────────────


async def test_exactly_one_run_control_is_created_and_every_component_shares_it(
    workspace, monkeypatch
):
    created: list[RunControl] = []
    original = RunControl.create.__func__  # type: ignore[attr-defined]

    def spy(cls, *a: Any, **k: Any):
        control = original(cls, *a, **k)
        created.append(control)
        return control

    monkeypatch.setattr(RunControl, "create", classmethod(spy))

    seen_by_limits: list[RunControl] = []
    real_init = ControlLimits.__init__

    def limits_init(self, control):
        seen_by_limits.append(control)
        real_init(self, control)

    monkeypatch.setattr(ControlLimits, "__init__", limits_init)

    from velune.orchestration.engine.acting import ActingAgentRunner

    seen_by_runner: list[RunControl] = []
    real_run = ActingAgentRunner.run_acting

    async def run_acting(self, spec, input, *, control, blocked_digests=frozenset()):
        seen_by_runner.append(control)
        return await real_run(self, spec, input, control=control, blocked_digests=blocked_digests)

    monkeypatch.setattr(ActingAgentRunner, "run_acting", run_acting)
    result = await go_pr(workspace, good_run(), asker(True))
    assert result.ok
    assert len(created) == 1  # the driver did not create a competing one
    assert seen_by_limits == created and seen_by_runner == created


async def test_a_control_for_another_run_is_refused_by_the_driver():
    from tests.council_core_fakes import CounterIds
    from velune.orchestration.engine import AgentRegistry
    from velune.orchestration.engine.driver import plan_review_specs

    class Runner:
        async def run(self, spec, input):
            raise AssertionError("must not run")

    driver = PlanReviewDriver(AgentRegistry(plan_review_specs()), Runner(), CounterIds())  # type: ignore[arg-type]
    control = RunControl.create("run-a", ExecutionBudget())
    with pytest.raises(DriverConfigError):
        await driver.run("task", run_id="run-b", control=control)


# ── one attempt budget ────────────────────────────────────────────────────────


def tight(monkeypatch, **limits: Any) -> None:
    def make(*, wall_clock_s: float) -> ExecutionBudget:
        return ExecutionBudget(wall_clock_s=wall_clock_s, **limits)

    monkeypatch.setattr(native_session, "ExecutionBudget", make)


async def test_the_model_call_budget_stops_the_run_before_another_provider_request(
    workspace, monkeypatch
):
    tight(monkeypatch, max_model_calls=2)
    provider = good_run()
    result = await go_pr(workspace, provider, asker(True))
    assert not result.ok and result.status == "budget_exhausted"
    assert result.failure_kind == "budget_exhausted"
    assert "request budget" in result.message
    assert len(provider.requests) == 2  # plan + the executor's first turn; the second was refused


async def test_the_provider_attempt_budget_stops_the_run_before_another_provider_request(
    workspace, monkeypatch
):
    tight(monkeypatch, max_provider_attempts=1)
    provider = good_run()
    result = await go_pr(workspace, provider, asker(True))
    assert not result.ok and result.status == "budget_exhausted"
    assert len(provider.requests) == 1  # the planner used the only attempt
    assert result.provider_attempts == 1


async def test_a_reviewer_refused_by_the_budget_is_budget_exhaustion_not_a_review_failure(
    workspace, monkeypatch
):
    tight(monkeypatch, max_model_calls=3)  # plan + two executor turns; the review is the fourth
    provider = good_run()
    result = await go_pr(workspace, provider, asker(True))
    assert result.status == "budget_exhausted" and len(provider.requests) == 3


async def test_a_provider_error_is_not_mistaken_for_budget_exhaustion(workspace):
    provider = ScriptedProvider(InferenceError("500 " + CANARY))
    result = await go_pr(workspace, provider, asker(True))
    assert (
        not result.ok
        and result.status == "failed"
        and result.failure_kind == "agent_provider_error"
    )
    assert CANARY not in result.message
    assert len(provider.requests) == 1  # the single retry owner is configured for one attempt


# ── whole-run deadline ────────────────────────────────────────────────────────


async def test_the_deadline_starts_before_planning_and_ends_a_slow_planner(workspace):
    provider = Timed((30.0, reply("1. plan")))
    started = time.monotonic()
    result = await asyncio.wait_for(go_pr(workspace, provider, asker(True), wall_clock_s=0.3), 10)
    elapsed = time.monotonic() - started
    assert not result.ok and result.status == "deadline" and result.failure_kind == "deadline"
    assert "time limit" in result.message
    assert elapsed < 3.0 and len(provider.requests) == 1  # no later step was started


async def test_the_deadline_is_not_reset_by_entering_the_next_stage(workspace):
    # plan takes 0.3 s of a 0.7 s run; the executor then hangs. If entering EXECUTE restarted the
    # clock the run would last about 1 s; it must stop at about 0.7 s.
    provider = Timed((0.3, reply("1. plan")), (30.0, reply("never")))
    started = time.monotonic()
    result = await asyncio.wait_for(go_pr(workspace, provider, asker(True), wall_clock_s=0.7), 10)
    elapsed = time.monotonic() - started
    assert result.status == "deadline"
    assert elapsed < 1.2
    assert len(provider.requests) == 2


async def test_the_deadline_during_review_ends_the_run_and_no_later_request_is_made(workspace):
    provider = Timed(
        (0.0, reply("1. plan")), (0.0, reply("a draft")), (30.0, reply("VERDICT: APPROVE"))
    )
    result = await asyncio.wait_for(go_pr(workspace, provider, asker(True), wall_clock_s=0.5), 10)
    assert result.status == "deadline" and len(provider.requests) == 3
    await asyncio.sleep(0.2)
    assert len(provider.requests) == 3  # nothing started after the run ended


async def test_a_deadline_is_a_result_and_cancellation_is_an_exception(workspace):
    provider = Timed((30.0, reply("x")))
    result = await asyncio.wait_for(go_pr(workspace, provider, asker(True), wall_clock_s=0.2), 10)
    assert result.status == "deadline"  # returned, not raised

    other = Timed((30.0, reply("x")))
    task = asyncio.ensure_future(go_pr(workspace, other, asker(True), wall_clock_s=60))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if other.requests:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)


# ── cancellation through every stage ──────────────────────────────────────────


async def cancel_when(task: asyncio.Future, provider: Any, requests: int) -> None:
    for _ in range(300):
        await asyncio.sleep(0.01)
        if len(provider.requests) >= requests:
            break
    assert len(provider.requests) >= requests
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)


async def test_cancelling_during_planning_stops_everything(workspace):
    provider = Timed((30.0, reply("x")))
    task = asyncio.ensure_future(go_pr(workspace, provider, asker(True)))
    await cancel_when(task, provider, 1)
    await asyncio.sleep(0.1)
    assert len(provider.requests) == 1


async def test_cancelling_during_execution_stops_before_any_tool_or_more_requests(workspace):
    provider = Timed((0.0, reply("1. plan")), (30.0, reply("x")))
    task = asyncio.ensure_future(go_pr(workspace, provider, asker(True)))
    await cancel_when(task, provider, 2)
    await asyncio.sleep(0.1)
    assert len(provider.requests) == 2


async def test_cancelling_during_review_stops_everything(workspace):
    provider = Timed((0.0, reply("1. plan")), (0.0, reply("draft")), (30.0, reply("x")))
    task = asyncio.ensure_future(go_pr(workspace, provider, asker(True)))
    await cancel_when(task, provider, 3)
    await asyncio.sleep(0.1)
    assert len(provider.requests) == 3


async def test_cancelling_at_the_approval_prompt_means_the_tool_never_runs(workspace):
    provider = good_run()
    result_seen: list[Any] = []

    async def cancel_at_prompt(prompt: Any) -> Any:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await go_pr(workspace, provider, cancel_at_prompt)
    result_seen.append(len(provider.requests))
    assert (
        len(provider.requests) == 2
    )  # plan and the executor's first turn; no result was sent back
    assert all("alpha contents" not in str(r.messages) for r in provider.requests)


# ── approval stays single-use and per call ────────────────────────────────────


@pytest.mark.parametrize("verdict", [False, None, "yes", 1])
async def test_a_denied_tool_call_ends_the_run_with_a_distinct_status(workspace, verdict):
    provider = good_run()
    result = await go_pr(workspace, provider, asker(verdict))
    assert not result.ok and result.status == "denied"
    assert result.failure_kind == "acting_approval_denied"
    assert len(provider.requests) == 2  # no request after the denial
    assert result.tool_calls == 0  # a denied call costs nothing and its body never ran
    assert all("alpha contents" not in str(r.messages) for r in provider.requests)


async def test_no_prompt_at_all_denies_every_tool_call(workspace):
    result = await go_pr(workspace, good_run(), None)
    assert not result.ok and result.status == "denied"


async def test_the_executor_has_only_read_file(workspace):
    provider = ScriptedProvider(
        reply("1. plan"), reply("", [read_call("a.txt")]), reply("done"), reply(APPROVE)
    )
    seen: list[Any] = []
    await go_pr(workspace, provider, asker(True, seen))
    assert len(seen) == 1 and "read_file" in seen[0].title


# ── reasoning cannot propose tools ────────────────────────────────────────────


async def test_a_planner_reply_that_contains_a_tool_call_fails_the_run_and_asks_nobody(workspace):
    provider = ScriptedProvider(reply("", [read_call("a.txt")]))
    seen: list[Any] = []
    result = await go_pr(workspace, provider, asker(True, seen))
    assert not result.ok and result.status == "failed"
    assert seen == [] and len(provider.requests) == 1


async def test_a_reviewer_reply_that_contains_a_tool_call_is_not_executed(workspace):
    provider = ScriptedProvider(reply("1. plan"), reply("a draft"), reply("", [read_call("b.txt")]))
    seen: list[Any] = []
    result = await go_pr(workspace, provider, asker(True, seen))
    assert not result.ok and seen == []


# ── secrets ───────────────────────────────────────────────────────────────────


def canary_scrub(text: str) -> str:
    return text.replace(CANARY, "[redacted]")


async def test_a_secret_in_a_model_artifact_never_reaches_a_later_request_or_the_answer(workspace):
    provider = ScriptedProvider(
        reply(f"1. use key {CANARY}"),
        reply(f"the key is {CANARY}"),
        reply(APPROVE),
    )
    result = await go_pr(workspace, provider, asker(True), scrub=canary_scrub)
    assert CANARY not in str([r.messages for r in provider.requests])
    assert CANARY not in (result.answer or "") and CANARY not in result.message


async def test_a_scrubber_that_cannot_be_built_prevents_the_run(workspace):
    def broken_scrubber() -> Any:
        raise RuntimeError("keystore locked " + CANARY)

    provider = good_run()
    scrub = SecretScrubber(static_source({CANARY: "x"}))
    assert callable(scrub)
    from velune.orchestration.engine_adapters import acting_stack

    original = acting_stack.credential_scrubber
    acting_stack.credential_scrubber = broken_scrubber  # type: ignore[assignment]
    try:
        with pytest.raises(NativeSessionError) as info:
            await go_pr(workspace, provider, asker(True))
    finally:
        acting_stack.credential_scrubber = original
    assert provider.requests == [] and CANARY not in str(info.value)


# ── ownership: one control creator, no artifact writes, no second retry or deadline owner ──


def _tree(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"))


SESSION = ROOT / "velune" / "orchestration" / "engine_adapters" / "native_session.py"
LIMITS = ROOT / "velune" / "orchestration" / "engine" / "limits.py"
INVOKER = ROOT / "velune" / "orchestration" / "engine_adapters" / "reasoning_invoker.py"
NATIVE_CLI = ROOT / "velune" / "cli" / "commands" / "native.py"


def test_the_session_creates_the_run_control_once_and_never_writes_artifacts_or_state():
    tree = _tree(SESSION)
    creates = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "create"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "RunControl"
    ]
    assert (
        len(creates) == 2
    )  # one in each mode's function; the plan/review one is the only one on that path
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"ArtifactStore", "record_artifact", "advance", "fail", "cancel"}
    assert not attrs & {"put", "record_artifact"}


def test_the_session_and_cli_own_no_deadline_timer_or_retry_loop_of_their_own():
    for path in (SESSION, NATIVE_CLI):
        tree = _tree(path)
        called = {
            n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
        }
        assert not called & {"wait_for", "timeout", "sleep", "call_later"}, path.name
        assert not any(isinstance(n, ast.While) for n in ast.walk(tree)), path.name


def test_the_limits_view_creates_nothing_and_owns_no_state():
    tree = _tree(LIMITS)
    called = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
    }
    assert "create" not in called and "CancellationToken" not in called
    assert "RunControl" not in {
        n.func.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


# ── the reasoning invoker bound to a run's limits ─────────────────────────────


def req(**kw: Any) -> ReasoningRequest:
    fields: dict[str, Any] = {
        "run_id": "run-1",
        "call_id": "call-1",
        "agent": "planner",
        "routing_role": "planner",
        "attempt": 1,
        "messages": ({"role": "system", "content": "sys"}, {"role": "user", "content": "task"}),
        "timeout_s": 5.0,
        "max_tokens": 50,
    }
    fields.update(kw)
    return ReasoningRequest(**fields)


def bound(provider: Any, control: RunControl, **kw: Any) -> ProviderReasoningInvoker:
    async def no_sleep(_: float) -> None:
        return None

    kw.setdefault("sleep", no_sleep)
    return ProviderReasoningInvoker.from_provider(
        provider, "model-x", limits=ControlLimits(control), **kw
    )


async def test_a_bound_call_reserves_one_model_call_and_one_attempt():
    control = RunControl.create("run-1", ExecutionBudget())
    provider = ScriptedProvider(reply("ok"))
    result = await bound(provider, control).invoke(req())
    usage = control.budget.usage()
    assert result.ok and (usage.model_calls, usage.provider_attempts) == (1, 1)


async def test_retries_cost_one_model_call_and_one_attempt_per_request_without_double_counting():
    control = RunControl.create("run-1", ExecutionBudget())
    provider = ScriptedProvider(InferenceError("boom"), reply("second try"))
    invoker = bound(provider, control, retry=ProviderRetryPolicy(max_attempts=3))
    result = await invoker.invoke(req())
    usage = control.budget.usage()
    assert result.ok and result.attempts == 2 and len(provider.requests) == 2
    assert (usage.model_calls, usage.provider_attempts) == (1, 2)


async def test_concurrent_calls_cannot_oversubscribe_the_attempt_budget():
    control = RunControl.create("run-1", ExecutionBudget(max_provider_attempts=2))
    provider = ScriptedProvider(reply("ok"))
    invoker = bound(provider, control)
    results = await asyncio.gather(*(invoker.invoke(req(call_id=f"c{i}")) for i in range(5)))
    assert len(provider.requests) == 2
    assert sum(r.ok for r in results) == 2
    refused = [r for r in results if not r.ok]
    assert all(r.status is SeatStatus.SKIPPED and r.error.message == BUDGET_SPENT for r in refused)
    assert control.budget.usage().provider_attempts == 2


async def test_a_spent_budget_sends_no_request():
    control = RunControl.create("run-1", ExecutionBudget(max_model_calls=1))
    provider = ScriptedProvider(reply("ok"))
    invoker = bound(provider, control)
    assert (await invoker.invoke(req())).ok
    second = await invoker.invoke(req(call_id="c2"))
    assert second.status is SeatStatus.SKIPPED and len(provider.requests) == 1


async def test_a_passed_deadline_sends_no_request_and_is_a_timeout_result():
    control = RunControl.create("run-1", ExecutionBudget(wall_clock_s=0.05))
    await asyncio.sleep(0.1)
    provider = ScriptedProvider(reply("ok"))
    result = await bound(provider, control).invoke(req())
    assert result.status is SeatStatus.TIMEOUT and result.error.message == DEADLINE_REACHED
    assert provider.requests == []


async def test_a_cancelled_run_sends_no_request_and_raises():
    control = RunControl.create("run-1", ExecutionBudget())
    control.token.cancel()
    provider = ScriptedProvider(reply("ok"))
    with pytest.raises(asyncio.CancelledError):
        await bound(provider, control).invoke(req())
    assert provider.requests == []


async def test_a_run_cancelled_while_the_call_was_in_flight_raises_after_it_returns():
    control = RunControl.create("run-1", ExecutionBudget())

    class CancelsRun:
        provider_id = "p"

        async def infer(self, request: Any) -> Any:
            control.token.cancel()
            return reply("late")

    with pytest.raises(asyncio.CancelledError):
        await bound(CancelsRun(), control).invoke(req())


async def test_the_call_timeout_is_clamped_to_what_remains_of_the_run():
    control = RunControl.create("run-1", ExecutionBudget(wall_clock_s=0.3))
    provider = Timed((30.0, reply("never")))
    started = time.monotonic()
    result = await asyncio.wait_for(bound(provider, control).invoke(req(timeout_s=60.0)), 10)
    assert result.status is SeatStatus.TIMEOUT and time.monotonic() - started < 3.0


async def test_the_unbound_invoker_still_counts_nothing():
    control = RunControl.create("run-1", ExecutionBudget())
    provider = ScriptedProvider(reply("ok"))
    result = await ProviderReasoningInvoker.from_provider(provider, "m").invoke(req())
    assert result.ok and control.budget.usage().provider_attempts == 0
