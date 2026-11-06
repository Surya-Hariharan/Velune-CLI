"""Phase 4E-8 Stage A: the production ``ReasoningInvoker`` over the provider acting adapter.

Offline only: a scripted fake provider, synthetic canary secrets, no network, no CLI, no credentials.
Reuses the provider-adapter test doubles unchanged.
"""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.council_core_fakes import CounterIds, FakeClock
from tests.test_engine_provider_acting import CATALOG, FakeProvider, call, response
from velune.core.errors.provider import (
    InferenceError,
    ProviderAuthenticationError,
    RateLimitError,
)
from velune.core.types.agent import AgentRole
from velune.orchestration.engine import AgentRegistry, ReasoningAgentRunner
from velune.orchestration.engine.driver import PlanReviewDriver, plan_review_specs
from velune.orchestration.engine.ports import (
    ActingInvoker,
    ActingRequest,
    ReasoningInvoker,
    ReasoningRequest,
    ReasoningResult,
    SeatStatus,
    StaticPromptResolver,
)
from velune.orchestration.engine_adapters import reasoning_invoker as ri
from velune.orchestration.engine_adapters.provider_acting import (
    ProviderActingInvoker,
    ProviderRetryPolicy,
    fixed_failure_messages,
)
from velune.orchestration.engine_adapters.reasoning_invoker import ProviderReasoningInvoker

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "velune" / "orchestration" / "engine_adapters" / "reasoning_invoker.py"
CANARY = "sk-CANARY-0123456789abcdef"
FIXED = fixed_failure_messages() | {ri.UNUSABLE_REPLY, ri.INVOKER_FAILED}


def request(**kw: Any) -> ReasoningRequest:
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


def invoker(provider: Any, **kw: Any) -> ProviderReasoningInvoker:
    kw.setdefault("sleep", _no_sleep)
    return ProviderReasoningInvoker.from_provider(provider, "model-x", **kw)


async def _no_sleep(_: float) -> None:
    return None


def scrub_canary(text: str) -> str:
    return text.replace(CANARY, "[redacted]")


# ── 1. a plain-text reply ─────────────────────────────────────────────────────


async def test_a_plain_text_reply_is_a_successful_result():
    provider = FakeProvider(response("1. read\n2. answer", stop=None))
    result = await invoker(provider).invoke(request())
    assert result.ok and result.text == "1. read\n2. answer"
    assert result.status is SeatStatus.OK and result.error is None
    assert len(provider.requests) == 1


async def test_the_provider_receives_the_messages_the_model_id_and_the_token_limit():
    provider = FakeProvider(response("ok", stop=None))
    await invoker(provider).invoke(request(max_tokens=77))
    sent = provider.requests[0]
    assert sent.model_id == "model-x" and sent.max_tokens == 77
    assert [(m["role"], m["content"]) for m in sent.messages] == [
        ("system", "sys"),
        ("user", "task"),
    ]


# ── 2. protocol conformance ───────────────────────────────────────────────────


def test_it_satisfies_the_reasoning_port_and_is_not_an_acting_invoker():
    inv = invoker(FakeProvider())
    assert isinstance(inv, ReasoningInvoker)
    assert not isinstance(
        inv, ActingInvoker
    )  # no .propose: it cannot be passed where tools are expected


def test_the_constructor_refuses_something_that_is_not_an_acting_invoker():
    with pytest.raises(TypeError):
        ProviderReasoningInvoker(object())  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ProviderReasoningInvoker(FakeProvider())  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["", "  ", None])
def test_the_model_id_must_be_a_real_string(bad):
    with pytest.raises(ValueError):
        ProviderReasoningInvoker.from_provider(FakeProvider(), bad)


# ── 3. no tools are ever offered ──────────────────────────────────────────────


async def test_no_tool_definitions_are_sent_even_when_the_wrapped_invoker_has_a_catalog():
    provider = FakeProvider(response("ok", stop=None))
    acting = ProviderActingInvoker(
        provider, "model-x", CATALOG
    )  # read_file, git_status, write_file
    await ProviderReasoningInvoker(acting).invoke(request())
    sent = provider.requests[0]
    assert sent.tools is None and sent.tool_choice is None


async def test_a_request_that_names_tools_cannot_make_the_adapter_offer_them():
    provider = FakeProvider(response("ok", stop=None))
    acting = ProviderActingInvoker(provider, "model-x", CATALOG)
    hostile = ActingRequest(**request().model_dump(), offered_tools=("read_file", "write_file"))
    await ProviderReasoningInvoker(acting).invoke(hostile)
    assert provider.requests[0].tools is None


def test_a_reasoning_request_has_no_field_for_tools_or_a_budget_gate():
    assert "offered_tools" not in ReasoningRequest.model_fields
    assert "attempt_gate" not in ReasoningRequest.model_fields


async def test_the_wrapped_invoker_always_sees_an_empty_tool_list_and_no_gate():
    seen: list[ActingRequest] = []

    class Spy:
        async def propose(self, req: ActingRequest):
            seen.append(req)
            from velune.orchestration.engine.ports import ActingResult

            return ActingResult.success(text="fine")

    await ProviderReasoningInvoker(Spy()).invoke(request())  # type: ignore[arg-type]
    assert [r.offered_tools for r in seen] == [()]
    assert seen[0].attempt_gate is None and seen[0].provider_retry_allowed is True


@pytest.mark.parametrize("reply", ["valid", "other_tool", "text_with_call"])
async def test_a_reply_that_contains_a_tool_call_is_a_failure_not_a_proposal(reply):
    calls = {
        "valid": [call()],  # a well-formed call to a tool that exists in the catalog
        "other_tool": [call(name="write_file", args={"path": "x"})],
        "text_with_call": [call()],
    }[reply]
    provider = FakeProvider(response("here", calls))
    acting = ProviderActingInvoker(provider, "model-x", CATALOG)
    result = await ProviderReasoningInvoker(acting).invoke(request())
    assert not result.ok and result.text is None
    assert result.error is not None and result.error.message in FIXED


async def test_a_wrapped_invoker_that_returns_proposals_is_rejected_as_a_second_check():
    from velune.orchestration.engine.ports import ActingResult
    from velune.orchestration.engine.proposals import ProposedToolCall

    proposal = ProposedToolCall(call_id="c1", name="read_file", arguments={"path": "a"})

    class Rogue:
        async def propose(self, req: ActingRequest):
            return ActingResult.success(text="hello", proposals=(proposal,))

    result = await ProviderReasoningInvoker(Rogue()).invoke(request())  # type: ignore[arg-type]
    assert not result.ok and result.error is not None and result.error.message == ri.UNUSABLE_REPLY


# ── 4. provider and model identity ────────────────────────────────────────────


async def test_the_result_names_exactly_the_configured_provider_and_model():
    provider = FakeProvider(response("ok", stop=None))
    result = await invoker(provider).invoke(request(routing_role="reviewer"))
    assert result.model is not None
    assert (result.model.provider_id, result.model.model_id) == ("fake", "model-x")
    assert result.fallback_used is False


async def test_the_routing_role_in_a_request_cannot_change_provider_or_model():
    provider = FakeProvider(response("ok", stop=None))
    inv = invoker(provider)
    for role in ("planner", "reviewer", "synthesizer", "coder"):
        await inv.invoke(request(routing_role=role))
    assert {r.model_id for r in provider.requests} == {"model-x"}
    assert len(provider.requests) == 4


async def test_a_failure_still_names_the_configured_model():
    provider = FakeProvider(raises=ProviderAuthenticationError("bad key"))
    result = await invoker(provider).invoke(request())
    assert result.model is not None and result.model.model_id == "model-x"


# ── 5. provider errors map to safe, typed failures ────────────────────────────


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (ProviderAuthenticationError(f"bad key {CANARY}"), SeatStatus.AUTH_ERROR),
        (InferenceError(f"500 body {CANARY}"), SeatStatus.PROVIDER_ERROR),
        (RateLimitError(f"slow down {CANARY}"), SeatStatus.PROVIDER_ERROR),
        (ValueError(f"unknown model {CANARY}"), SeatStatus.PROVIDER_ERROR),
    ],
)
async def test_provider_exceptions_become_typed_failures_with_fixed_text(exc, status):
    result = await invoker(FakeProvider(raises=exc), scrub=scrub_canary).invoke(request())
    assert not result.ok and result.status is status and result.text is None
    assert result.error is not None and result.error.message in FIXED
    assert CANARY not in repr(result) and CANARY not in result.model_dump_json()


@pytest.mark.parametrize(
    "bad",
    [None, "just text", {"content": "x"}, 42, object()],
)
async def test_a_response_that_is_not_an_inference_response_is_a_failure(bad):
    result = await invoker(FakeProvider(bad)).invoke(request())
    assert not result.ok and result.error is not None and result.error.message in FIXED


@pytest.mark.parametrize("content", ["", "   \n\t "])
async def test_an_empty_reply_is_a_failure(content):
    result = await invoker(FakeProvider(response(content, stop=None))).invoke(request())
    assert result.status is SeatStatus.EMPTY and result.text is None


@pytest.mark.parametrize("finish", ["length", "max_tokens", "content_filter", "refusal"])
async def test_truncated_or_refused_replies_are_failures(finish):
    result = await invoker(FakeProvider(response("partial", finish=finish, stop=None))).invoke(
        request()
    )
    assert not result.ok and result.text is None


async def test_an_unknown_stop_reason_is_a_failure_not_a_guess():
    resp = response("text", finish="something_new", stop=None)
    result = await invoker(FakeProvider(resp)).invoke(request())
    assert not result.ok


async def test_an_oversized_request_is_refused_without_calling_the_provider():
    provider = FakeProvider(response("ok", stop=None))
    big = request(messages=({"role": "user", "content": "x" * 500},))
    result = await invoker(provider, max_request_chars=100).invoke(big)
    assert not result.ok and provider.requests == []


async def test_an_oversized_reply_is_a_failure_not_a_truncation():
    result = await invoker(
        FakeProvider(response("y" * 500, stop=None)), max_response_chars=100
    ).invoke(request())
    assert not result.ok and result.text is None


async def test_a_request_that_is_not_a_reasoning_request_is_a_failure_not_a_crash():
    result = await invoker(FakeProvider()).invoke(object())  # type: ignore[arg-type]
    assert not result.ok and result.error is not None and result.error.message == ri.INVOKER_FAILED


async def test_a_wrapped_invoker_that_raises_is_a_fixed_failure():
    class Boom:
        async def propose(self, req: ActingRequest):
            raise RuntimeError(f"internal {CANARY}")

    result = await ProviderReasoningInvoker(Boom()).invoke(request())  # type: ignore[arg-type]
    assert not result.ok and result.error is not None and result.error.message == ri.INVOKER_FAILED
    assert CANARY not in repr(result)


# ── 6. cancellation and deadlines ─────────────────────────────────────────────


async def test_a_cancelled_provider_call_propagates_as_cancellation():
    provider = FakeProvider(raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await invoker(provider).invoke(request())
    assert len(provider.requests) == 1


async def test_cancelling_the_task_during_a_provider_call_stops_it_and_sends_nothing_more():
    class Slow:
        provider_id = "slow"

        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def infer(self, req: Any):
            self.requests.append(req)
            await asyncio.sleep(3600)

    provider = Slow()
    task = asyncio.ensure_future(invoker(provider).invoke(request()))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if provider.requests:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert len(provider.requests) == 1


async def test_a_deadline_is_a_timeout_result_and_not_a_cancellation():
    class Slow:
        provider_id = "slow"

        async def infer(self, req: Any):
            await asyncio.sleep(3600)

    result = await asyncio.wait_for(invoker(Slow()).invoke(request(timeout_s=0.1)), timeout=5)
    assert result.status is SeatStatus.TIMEOUT and result.text is None


# ── 7. attempts: reported, never double-counted, one retry owner ──────────────


async def test_the_attempt_count_of_a_successful_call_is_one():
    result = await invoker(FakeProvider(response("ok", stop=None))).invoke(request())
    assert result.attempts == 1


async def test_a_transient_error_is_retried_by_the_wrapped_invoker_and_the_count_is_reported():
    class Flaky:
        provider_id = "flaky"

        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def infer(self, req: Any):
            self.requests.append(req)
            if len(self.requests) == 1:
                raise InferenceError("boom")
            return response("second try", stop=None)

    provider = Flaky()
    result = await invoker(provider).invoke(request())
    assert result.ok and result.text == "second try"
    assert len(provider.requests) == 2 and result.attempts == 2


async def test_retries_are_not_nested_when_the_registry_wrapper_is_present():
    from velune.providers.retrying import RetryingProvider

    class Down:
        provider_id = "down"

        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def infer(self, req: Any):
            self.requests.append(req)
            raise InferenceError("down")

    inner = Down()
    wrapped = RetryingProvider(inner)  # type: ignore[arg-type]
    result = await invoker(wrapped, retry=ProviderRetryPolicy(max_attempts=3)).invoke(request())
    assert not result.ok
    assert len(inner.requests) == 3  # one retry owner: not 3 x 3
    assert result.attempts == 3


async def test_a_single_attempt_policy_sends_one_request_and_no_fallback_provider_is_used():
    primary = FakeProvider(raises=InferenceError("down"))
    other = FakeProvider(response("from another provider", stop=None))
    result = await invoker(primary, retry=ProviderRetryPolicy(max_attempts=1)).invoke(request())
    assert not result.ok and len(primary.requests) == 1
    assert other.requests == []  # nothing else exists to be asked, and nothing is
    assert result.fallback_used is False


async def test_the_adapter_never_reserves_on_a_run_budget():
    # The reasoning port carries no budget gate, so there is nothing to reserve against and no
    # second counter exists. The reported attempts are what a budget owner can account for.
    inv = invoker(FakeProvider(response("ok", stop=None)))
    names = {n for n in dir(inv) if not n.startswith("__")}
    assert not names & {"reserve", "budget", "control", "attempt_gate", "token", "cancel"}


# ── 8. secrets ────────────────────────────────────────────────────────────────


async def test_a_secret_in_a_provider_bound_message_is_scrubbed():
    provider = FakeProvider(response("ok", stop=None))
    msgs = ({"role": "system", "content": "sys"}, {"role": "user", "content": f"use {CANARY}"})
    await invoker(provider, scrub=scrub_canary).invoke(request(messages=msgs))
    assert CANARY not in str([m["content"] for m in provider.requests[0].messages])


async def test_a_system_prompt_the_scrubber_would_change_is_refused_and_nothing_is_sent():
    provider = FakeProvider(response("ok", stop=None))
    msgs = ({"role": "system", "content": f"sys {CANARY}"}, {"role": "user", "content": "task"})
    result = await invoker(provider, scrub=scrub_canary).invoke(request(messages=msgs))
    assert not result.ok and provider.requests == []
    assert CANARY not in repr(result)


async def test_a_secret_in_the_reply_is_scrubbed_in_the_result():
    provider = FakeProvider(response(f"the key is {CANARY}", stop=None))
    result = await invoker(provider, scrub=scrub_canary).invoke(request())
    assert result.ok and CANARY not in (result.text or "") and "[redacted]" in (result.text or "")


async def test_a_scrubber_that_fails_closes_the_call():
    def broken(_: str) -> str:
        raise RuntimeError("scrubber down")

    provider = FakeProvider(response("ok", stop=None))
    result = await invoker(provider, scrub=broken).invoke(request())
    assert not result.ok and provider.requests == []


# ── 9. composition with the existing runner and driver (offline) ──────────────

PROMPTS = StaticPromptResolver(
    {"engine.plan": "You plan.", "engine.execute": "You execute.", "engine.review": "You review."}
)


def reasoning_runner(provider: Any) -> ReasoningAgentRunner:
    return ReasoningAgentRunner(
        invoker(provider), PROMPTS, clock=FakeClock(), ids=CounterIds(), grace_s=0.0
    )


async def test_the_reasoning_runner_runs_planner_and_reviewer_specs_through_the_adapter():
    from velune.council.domain import ArtifactKind
    from velune.orchestration.engine.runner import AgentInput, ContextItem

    planner, _, reviewer = plan_review_specs()
    provider = FakeProvider(response("1. step", stop=None))
    runner = reasoning_runner(provider)
    item = ContextItem(kind=ArtifactKind.QUESTION, text="the task")
    for spec in (planner, reviewer):
        out = await runner.run(
            spec, AgentInput(run_id="run-1", instruction="do it", context=(item,))
        )
        assert out.ok and out.text == "1. step"
        assert out.role in (AgentRole.PLANNER, AgentRole.REVIEWER)
    assert len(provider.requests) == 2
    assert all(r.tools is None for r in provider.requests)


async def test_the_plan_review_driver_completes_a_reasoning_only_run_through_the_adapter():
    script = iter(
        [
            response("1. answer it", stop=None),
            response("The answer is 42.", stop=None),
            response("VERDICT: APPROVE\nfine", stop=None),
        ]
    )

    class Scripted:
        provider_id = "scripted"

        def __init__(self) -> None:
            self.requests: list[Any] = []

        async def infer(self, req: Any):
            self.requests.append(req)
            return next(script)

    provider = Scripted()
    driver = PlanReviewDriver(
        AgentRegistry(plan_review_specs()), reasoning_runner(provider), CounterIds()
    )
    result = await driver.run("what is the answer?")
    assert result.answer == "The answer is 42."
    assert result.state.status.value == "succeeded"
    assert len(provider.requests) == 3  # plan, execute, review: one provider request each
    assert all(r.tools is None and r.model_id == "model-x" for r in provider.requests)


# ── 10. isolation ─────────────────────────────────────────────────────────────


def _imports() -> tuple[set[str], set[str]]:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    modules: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
    return modules, names


def test_the_source_imports_no_council_cli_tool_permission_or_execution_code():
    modules, names = _imports()
    banned = (
        "velune.council",
        "velune.cli",
        "velune.tools",
        "velune.permissions",
        "velune.providers",
        "velune.orchestration.engine.toolexec",
        "velune.orchestration.engine.approval",
        "velune.orchestration.engine.acting",
        "velune.orchestration.engine.driver",
        "velune.orchestration.engine.artifacts",
        "velune.orchestration.engine.execcontrol",
        "velune.orchestration.engine.flag",
        "subprocess",
        "socket",
        "httpx",
        "os",
    )
    for module in modules:
        assert not module.startswith(banned), module
    forbidden = {
        "RunControl",
        "CancellationToken",
        "ExecutionBudget",
        "ApprovalLedger",
        "RetryingProvider",
    }
    assert not names & forbidden


def test_the_source_has_no_retry_loop_sleep_or_provider_call_of_its_own():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.While) for n in ast.walk(tree))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attrs & {"infer", "stream", "sleep", "execute", "consume", "issue", "put"}


def _loaded(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_importing_the_adapter_loads_no_provider_tool_permission_cli_or_executor_code():
    out = _loaded(
        "import sys\n"
        "import velune.orchestration.engine_adapters.reasoning_invoker\n"
        "bad = ('velune.orchestration.engine.acting','velune.orchestration.engine.toolexec',"
        "'velune.permissions','velune.tools','velune.providers','velune.cli','httpx')\n"
        "print('LOADED', sorted(m for m in sys.modules if m.startswith(bad)))\n"
    )
    assert out == "LOADED []", out


def test_the_engine_package_does_not_load_or_export_the_adapter():
    import velune.orchestration.engine as engine

    assert not any("ProviderReasoning" in n or "reasoning_invoker" in n for n in dir(engine))
    assert "reasoning_invoker" not in Path(engine.__file__).read_text(encoding="utf-8")
    out = _loaded(
        "import sys\nimport velune.orchestration.engine\n"
        "print('LOADED', 'velune.orchestration.engine_adapters.reasoning_invoker' in sys.modules)\n"
    )
    assert out == "LOADED False"


def test_only_the_native_session_imports_the_adapter():
    offenders = []
    for path in (ROOT / "velune").rglob("*.py"):
        if path == SOURCE:
            continue
        if "reasoning_invoker" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == ["velune/orchestration/engine_adapters/native_session.py"]


def test_the_adapter_result_type_is_the_ports_reasoning_result():
    assert ReasoningResult.success("x").ok
