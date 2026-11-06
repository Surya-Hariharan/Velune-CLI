"""Phase 4D-4d: end-to-end runs of the native engine, plus the cancellation/artifact lifecycle.

Everything is a fake or a synthetic read-only tool; the real pieces under test are the driver, the
acting runner, the trusted executor, the approval ledger and the run control. Synchronisation uses
events, never sleeps. An autouse fixture makes any attempt to spawn a process or open a network
connection fail, and each test checks that its workspace is still empty.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from typing import Any

import pytest

from tests.council_core_fakes import CounterIds, FakeClock
from tests.test_engine_acting_driver import (
    APPROVE,
    REVISE,
    TASK,
    TOOL_TEXT,
    HangTool,
    mk,
    reasoning_invoker,
    rig,
    text_of,
)
from tests.test_engine_acting_runner import FakeActingInvoker, calls, proposal
from tests.test_engine_tool_executor import SECRET, ApprovePort, Registry, SpyTool
from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind
from velune.kernel.config import VeluneConfig
from velune.orchestration.engine import (
    AgentInput,
    AgentRegistry,
    ArtifactOrigin,
    ArtifactStore,
    EngineDisabledError,
    PlanReviewDriver,
    ReasoningAgentRunner,
    RunHandle,
    StaticPromptResolver,
    create_plan_review_driver,
    plan_review_specs,
)
from velune.orchestration.engine.execcontrol import ExecutionBudget, InvocationState, RunControl
from velune.orchestration.engine.toolexec import (
    ToolOutcome,
    ToolStatus,
    TrustedToolExecutor,
)
from velune.orchestration.state import RunPhase, RunStatus

PROMPTS = StaticPromptResolver(
    {"engine.plan": "You plan.", "engine.execute": "You execute.", "engine.review": "You review."}
)
REQUEST_1 = "run-1.a1.t1"


@pytest.fixture(autouse=True)
def no_side_effects(monkeypatch):
    def refuse(*a: Any, **k: Any):
        raise AssertionError("an E2E test attempted a real external side effect")

    monkeypatch.setattr(subprocess.Popen, "__init__", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr("os.system", refuse)


@pytest.fixture
def puts(monkeypatch):
    log: list[tuple[str, Any]] = []
    real = ArtifactStore.put

    def spy(self, **kw):
        item = real(self, **kw)
        log.append((sys._getframe(1).f_globals["__name__"], item))
        return item

    monkeypatch.setattr(ArtifactStore, "put", spy)
    return log


async def run(r, run_id: str = "run-1"):
    return await r.driver.run(TASK, run_id=run_id, handle=r.handle)


def evidence(puts):
    return [a for _, a in puts if a.kind is ArtifactKind.EVIDENCE]


# ── 1. reasoning-only plan/review ─────────────────────────────────────────────


async def test_e1_reasoning_only_plan_and_review_run(tmp_path):
    reason, seen = reasoning_invoker([APPROVE])
    driver = PlanReviewDriver(
        AgentRegistry(plan_review_specs()),
        ReasoningAgentRunner(reason, PROMPTS, clock=FakeClock(), ids=CounterIds()),
        CounterIds(),
    )
    out = await driver.run(TASK)
    assert out.state.status is RunStatus.SUCCEEDED and out.answer
    assert [p.value for p in out.state.trail] == [
        "plan",
        "execute",
        "observe",
        "reflect",
        "validate",
        "finish",
    ]
    assert (out.abandoned_count, out.unsettled_calls) == (0, 0)
    assert {q.agent for q in seen} == {AgentRole.PLANNER, AgentRole.REASONER, AgentRole.REVIEWER}
    assert list(tmp_path.iterdir()) == []


# ── 2 + 13. a successful read-only tool and the provenance it leaves ──────────


async def test_e2_and_e13_acting_run_with_a_read_only_tool_preserves_provenance(tmp_path, puts):
    r = rig(tmp_path)
    out = await run(r)
    assert out.state.status is RunStatus.SUCCEEDED and out.answer == "the answer"
    assert r.tool.runs == 1
    (item,) = evidence(puts)
    assert item.origin is ArtifactOrigin.TOOL and item.author is None
    assert item.source == "read_file" and item.text == TOOL_TEXT
    assert (item.tool_meta.request_id, item.tool_meta.status, item.tool_meta.outcome) == (
        REQUEST_1,
        "ok",
        "completed",
    )
    assert item.tool_meta.executed and not item.tool_meta.unsettled
    assert item.phase is RunPhase.EXECUTE and item.attempt == 1
    ctl = r.spy.controls[0]
    assert ctl.budget.usage().calls == 1 and ctl.abandoned_count == 0
    assert ctl.store.state_of(REQUEST_1) is InvocationState.COMPLETED
    assert (out.abandoned_count, out.unsettled_calls) == (0, 0)
    assert list(tmp_path.iterdir()) == []


# ── 3. several execution turns ────────────────────────────────────────────────


async def test_e3_multiple_turns_use_engine_request_ids_in_order(tmp_path):
    r = rig(
        tmp_path,
        acting=FakeActingInvoker(
            calls(proposal(call_id="m1", file_path="a.txt")),
            calls(proposal(call_id="m1", file_path="b.txt")),
            "final",
        ),
    )
    out = await run(r)
    assert out.state.status is RunStatus.SUCCEEDED and out.answer == "final"
    ctl = r.spy.controls[0]
    assert r.tool.runs == 2 and ctl.budget.usage().calls == 2
    assert ctl.store.state_of("run-1.a1.t1") is InvocationState.COMPLETED
    assert ctl.store.state_of("run-1.a1.t2") is InvocationState.COMPLETED
    # The third model call saw both results, framed, after the trusted instruction.
    last = r.invoker.requests[-1].messages[1].content
    assert last.count("UNTRUSTED TOOL OUTPUT") == 2


# ── 4 + 5. retry reuses the control; the budget spans attempts ────────────────


async def test_e4_a_retry_reuses_the_control_and_its_ledgers(tmp_path):
    r = rig(
        tmp_path,
        acting=FakeActingInvoker(
            calls(proposal()), "one", calls(proposal(file_path="b.txt")), "two"
        ),
        reviews=(REVISE, APPROVE),
    )
    out = await run(r)
    assert out.state.status is RunStatus.SUCCEEDED and out.answer == "two"
    first, second = r.spy.controls
    assert first is second
    assert first.budget.usage().calls == 2
    assert {first.store.state_of("run-1.a1.t1"), first.store.state_of("run-1.a2.t1")} == {
        InvocationState.COMPLETED
    }
    assert out.state.attempt == 2


async def test_e5_budget_exhaustion_across_attempts_fails_closed(tmp_path):
    r = rig(
        tmp_path,
        acting=FakeActingInvoker(calls(proposal()), "one", calls(proposal(file_path="b.txt"))),
        reviews=(REVISE, APPROVE),
        budget=ExecutionBudget(max_tool_calls=1),
    )
    out = await run(r)
    assert out.state.status is RunStatus.FAILED
    assert out.state.failure.kind == "acting_budget_refused" and out.state.attempt == 2
    assert r.tool.runs == 1 and r.spy.controls[0].budget.usage().calls == 1


# ── 6 + 7. tool failure and approval denial ───────────────────────────────────


async def test_e6_a_failing_tool_ends_the_run_without_a_retry_or_a_leak(tmp_path, puts):
    tool = SpyTool(raises=RuntimeError(f"disk exploded {SECRET}"))
    r = rig(tmp_path, tool=tool)
    out = await run(r)
    assert out.state.failure.kind == "acting_tool_failed" and out.answer is None
    assert tool.runs == 1 and out.unsettled_calls == 0
    (item,) = evidence(puts)
    assert item.tool_meta.outcome == "failed_during_execution" and item.tool_meta.executed
    assert "disk exploded" not in item.text and SECRET not in item.text
    assert SECRET not in out.state.failure.message
    assert {q.agent for q in r.seen} == {AgentRole.PLANNER}


async def test_e7_approval_denial_means_the_tool_never_runs(tmp_path, puts):
    tool = SpyTool(result=TOOL_TEXT)
    executor = TrustedToolExecutor(Registry(tool), tmp_path, ApprovePort(approve=False))
    r = rig(tmp_path, tool=tool, executor=executor)
    out = await run(r)
    assert out.state.failure.kind == "acting_approval_denied" and tool.runs == 0
    (item,) = evidence(puts)
    assert item.tool_meta.status == "refused" and not item.tool_meta.executed
    assert r.spy.controls[0].abandoned_count == 0


async def test_e7b_the_default_headless_executor_denies_everything(tmp_path):
    tool = SpyTool(result=TOOL_TEXT)
    r = rig(tmp_path, tool=tool, executor=TrustedToolExecutor(Registry(tool), tmp_path))
    out = await run(r)
    assert out.state.failure.kind == "acting_approval_denied" and tool.runs == 0


# ── 8. unknown / unsettled execution ──────────────────────────────────────────


async def test_e8_a_tool_that_ends_cancelled_is_unknown_and_blocks_everything_after(tmp_path, puts):
    class Dies(SpyTool):
        async def execute(self, **kwargs):
            self.runs += 1
            raise asyncio.CancelledError

    tool = Dies()
    r = rig(tmp_path, tool=tool, max_attempts=3)
    out = await run(r)
    assert out.state.failure.kind == "acting_outcome_unknown" and out.unsettled_calls == 1
    assert out.state.attempt == 1 and tool.runs == 1  # not retried, no new attempt
    assert RunPhase.OBSERVE not in out.state.trail and RunPhase.REFLECT not in out.state.trail
    assert {q.agent for q in r.seen} == {AgentRole.PLANNER}
    (item,) = evidence(puts)
    assert item.tool_meta.unsettled and item.tool_meta.outcome == "outcome_unknown"
    assert r.spy.controls[0].store.state_of(REQUEST_1) is InvocationState.UNKNOWN


# ── 9. cancellation with a hanging tool ───────────────────────────────────────


async def test_e9_cancelling_the_driver_leaves_a_hanging_tool_tracked_not_stopped(tmp_path, puts):
    tool = HangTool()
    tool.result = TOOL_TEXT
    r = rig(tmp_path, tool=tool)
    task = asyncio.create_task(run(r))
    await asyncio.wait_for(tool.started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    ctl = r.spy.controls[0]
    assert (
        r.handle.state.status is RunStatus.CANCELLED and r.handle.state.phase is RunPhase.CANCELLED
    )
    assert ctl.token.cancelled and r.handle.abandoned_count == 1 and ctl.abandoned_count == 1
    assert not tool.gate.is_set()  # still running: nothing here claims it stopped
    assert ctl.store.state_of(REQUEST_1) is InvocationState.UNKNOWN
    assert evidence(puts) == []  # no result existed to store
    tool.gate.set()
    await ctl.drain()
    assert ctl.abandoned_count == 0 and tool.runs == 1


# ── 10. cancellation after a completed tool result ────────────────────────────


class CancelsWhileFinishing(SpyTool):
    """Completes, and signals the run's cancellation just as it returns."""

    holder: list[Any]

    async def execute(self, **kwargs):
        self.runs += 1
        self.holder[0].token.cancel()
        return TOOL_TEXT


async def test_e10_cancellation_after_a_completed_result_keeps_it_completed_and_still_cancels(
    tmp_path, puts
):
    tool = CancelsWhileFinishing()
    tool.holder = []
    r = rig(tmp_path, tool=tool)
    original = r.spy.run_acting

    async def capture(spec, input, *, control, blocked_digests=frozenset()):
        tool.holder.append(control)
        try:
            return await original(spec, input, control=control, blocked_digests=blocked_digests)
        except asyncio.CancelledError as exc:
            r.cancel_records = exc.tool_records
            raise

    r.spy.run_acting = capture
    with pytest.raises(asyncio.CancelledError):
        await run(r)
    (kept,) = r.cancel_records
    assert kept.outcome is ToolOutcome.COMPLETED and kept.status is ToolStatus.OK
    assert kept.output == TOOL_TEXT and kept.executed and not kept.unsettled
    assert r.handle.state.status is RunStatus.CANCELLED
    assert (r.handle.unsettled_calls, r.handle.abandoned_count) == (0, 0)
    assert tool.holder[0].store.state_of(REQUEST_1) is InvocationState.COMPLETED
    # Deliberate: the run ended, so the record is not stored (the store is per-run and discarded).
    assert evidence(puts) == []


# ── 11. duplicate digest handling ─────────────────────────────────────────────


async def test_e11_a_settled_call_may_repeat_but_an_unsettled_digest_is_never_run_again(
    tmp_path, puts
):
    # Settled calls with the same digest run again under fresh request ids (a legitimate re-read)...
    r = rig(
        tmp_path,
        acting=FakeActingInvoker(calls(proposal()), calls(proposal(call_id="m2")), "done"),
    )
    out = await run(r)
    assert out.state.status is RunStatus.SUCCEEDED and r.tool.runs == 2
    digests = {a.tool_meta.call_digest for a in evidence(puts)}
    assert len(digests) == 1  # the same call twice, two request ids

    # ...while a digest the driver tracks as unsettled is refused before the executor is reached,
    # whatever request id the next attempt would use.
    digest = digests.pop()
    tool = SpyTool(result=TOOL_TEXT)
    registry = Registry(tool)
    runner = mk(
        FakeActingInvoker(calls(proposal())),
        TrustedToolExecutor(registry, tmp_path, ApprovePort()),
        registry,
    )
    blocked = await runner.run_acting(
        plan_review_specs(("read_file",))[1],
        AgentInput(run_id="run-9", attempt=2, instruction="x", context=()),
        control=RunControl.create("run-9"),
        blocked_digests=frozenset({digest}),
    )
    assert blocked.failure.kind.value == "acting_duplicate_digest"
    assert tool.runs == 0 and blocked.tool_records == ()


# ── 12. planner / reviewer isolation ──────────────────────────────────────────


async def test_e12_only_the_acting_reasoner_ever_receives_tool_evidence(tmp_path):
    r = rig(
        tmp_path,
        acting=FakeActingInvoker(calls(proposal()), "one", "two"),
        reviews=(REVISE, APPROVE),
    )
    await run(r)
    assert r.seen and all(TOOL_TEXT not in text_of(q) for q in r.seen)
    assert any(TOOL_TEXT in text_of(q) for q in r.invoker.requests)
    attempt2 = r.spy.inputs[1]
    assert [i.origin for i in attempt2.context if i.kind is ArtifactKind.EVIDENCE] == [
        ArtifactOrigin.TOOL
    ]


# ── 14. the feature flag ──────────────────────────────────────────────────────


async def test_e14_flag_off_blocks_the_entry_point_and_on_runs_a_full_acting_run(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("VELUNE_ORCHESTRATION__ENGINE", raising=False)
    r = rig(tmp_path)
    args = (AgentRegistry(plan_review_specs(("read_file",))), None, CounterIds())
    with pytest.raises(EngineDisabledError):
        create_plan_review_driver(VeluneConfig(), *args, acting_runner=r.spy)  # type: ignore[arg-type]
    assert r.tool.runs == 0 and r.spy.phases == []

    reason, _ = reasoning_invoker([APPROVE])
    driver = create_plan_review_driver(
        VeluneConfig(orchestration={"engine": "on"}),
        AgentRegistry(plan_review_specs(("read_file",))),
        ReasoningAgentRunner(reason, PROMPTS, clock=FakeClock(), ids=CounterIds()),
        CounterIds(),
        acting_runner=r.spy,
    )
    out = await driver.run(TASK, run_id="run-1", handle=r.handle)
    assert out.state.status is RunStatus.SUCCEEDED and r.tool.runs == 1


# ── 15. isolation between runs ────────────────────────────────────────────────


async def test_e15_a_cancelled_run_leaks_nothing_into_the_next_run(tmp_path):
    first = CancelsWhileFinishing()
    first.holder = []
    r = rig(tmp_path, tool=first)
    original = r.spy.run_acting

    async def capture(spec, input, *, control, blocked_digests=frozenset()):
        first.holder.append(control)
        return await original(spec, input, control=control, blocked_digests=blocked_digests)

    r.spy.run_acting = capture
    with pytest.raises(asyncio.CancelledError):
        await run(r)
    # A second run through the same driver: new control, new store, and no trace of the first.
    r.spy.run_acting = original
    first.holder.clear()
    r.invoker._script.clear()
    r.invoker._script.extend(["fresh answer"])
    r.invoker.requests.clear()
    r.handle = RunHandle()
    r.spy.handle = r.handle
    out = await r.driver.run(TASK, run_id="run-2", handle=r.handle)
    assert out.state.status is RunStatus.SUCCEEDED and out.answer == "fresh answer"
    assert r.spy.controls[-1].run_id == "run-2" and r.spy.controls[-1] is not r.spy.controls[0]
    assert all(TOOL_TEXT not in text_of(q) for q in r.invoker.requests)
    assert r.spy.controls[-1].token.cancelled is False
    assert list(tmp_path.iterdir()) == []


def test_the_flag_is_still_off_by_default(monkeypatch):
    monkeypatch.delenv("VELUNE_ORCHESTRATION__ENGINE", raising=False)
    assert VeluneConfig().orchestration.engine == "off"


def test_failure_kinds_are_stable_and_value_free():
    from velune.orchestration.engine.acting import ActingFailureKind

    for kind in ActingFailureKind:
        assert kind.value.startswith("acting_") and len(kind.value) <= 40
