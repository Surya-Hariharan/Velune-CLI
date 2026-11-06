"""One native task, end to end, for a caller that already knows which provider and model to use.

This is the integration seam the CLI calls. It assembles nothing new: it checks the feature flag,
looks the provider up by the id it was given (no default, no substitute), builds the existing acting
stack (one credential-aware scrubber, the interactive approval port, retry ownership in the invoker)
and runs a single read-only acting agent under one :class:`RunControl`.

Guarantees, each covered by tests:

* the ``orchestration.engine`` flag is checked first; while it is off nothing is built or called;
* a provider that cannot be found, or a model that is not named, is an error, never a different one;
* approval is whatever ``ask`` says for that one invocation; anything but a literal ``True`` denies;
* what ``ask`` is shown is plain text with control and terminal-escape characters removed;
* cancellation marks the run cancelled so no further tool or provider request starts, and then
  propagates; it is never turned into a result;
* every failure is reported as a stable, fixed message. No exception text, provider body or
  credential reaches the returned value.
"""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from velune.orchestration.engine.acting import ActingFailureKind
from velune.orchestration.engine.driver import RunHandle
from velune.orchestration.engine.entry import create_plan_review_driver
from velune.orchestration.engine.errors import EngineDisabledError
from velune.orchestration.engine.execcontrol import ExecutionBudget, RunControl
from velune.orchestration.engine.flag import engine_enabled
from velune.orchestration.engine.limits import ControlLimits
from velune.orchestration.engine.native_task import (
    MonotonicClock,
    NativeTaskError,
    RandomIds,
    clean_task,
    native_prompts,
    plan_review_agents,
    plan_review_prompts,
    read_only_agent,
    task_input,
)
from velune.orchestration.engine.registry import AgentRegistry
from velune.orchestration.engine.runner import ReasoningAgentRunner
from velune.orchestration.engine_adapters.acting_stack import build_acting_stack
from velune.orchestration.engine_adapters.approval_port import (
    DEFAULT_PROMPT_TIMEOUT_S,
    ApprovalPrompt,
    AskCallable,
    InteractiveApprovalPort,
)
from velune.orchestration.engine_adapters.provider_acting import NO_RETRY
from velune.orchestration.engine_adapters.reasoning_invoker import ProviderReasoningInvoker
from velune.orchestration.state import RunStatus

DEFAULT_WALL_CLOCK_S = 120.0
DEFAULT_REVIEW_ATTEMPTS = 2  # the first draft plus at most one revision
MAX_PROMPT_LINE_CHARS = 400


class NativeSessionError(Exception):
    """A native run could not start. The message is fixed text and never carries a value."""


@dataclass(frozen=True)
class NativeRunResult:
    ok: bool
    text: str | None
    message: str  # stable; "" on success
    failure_kind: str | None
    turns: int
    tools: tuple[tuple[str, str], ...]  # (tool name, status) per call, in order
    provider_id: str
    model_id: str


def sanitize_text(text: object, limit: int = MAX_PROMPT_LINE_CHARS) -> str:
    """Printable text only: control, format (bidi) and escape characters become ``?``."""
    out: list[str] = []
    for char in str(text):
        if char in "\n\t":
            out.append(char if char == "\n" else " ")
        elif unicodedata.category(char) in {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}:
            out.append("?")
        else:
            out.append(char)
        if len(out) >= limit:
            out.append("…")
            break
    return "".join(out)


def safe_ask(ask: AskCallable | None) -> AskCallable | None:
    """Wrap a UI prompt so it only ever receives sanitized plain text."""
    if ask is None:
        return None

    async def wrapped(prompt: ApprovalPrompt) -> Any:
        return await ask(
            ApprovalPrompt(
                title=sanitize_text(prompt.title, 200),
                detail=sanitize_text(prompt.detail, 1200),
                hint=sanitize_text(prompt.hint, 300),
            )
        )

    return wrapped


def _clean_id(value: str, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise NativeSessionError(f"a {what} is required; none was chosen")
    return value.strip()


def _lookup(providers: Any, provider_id: str) -> Any:
    """Exactly one registry lookup by the id given. No default, no substitute."""
    try:
        return providers.get_or_raise(provider_id)
    except Exception:
        raise NativeSessionError(
            "the chosen provider is not available; it was not replaced with another"
        ) from None


def _read_only_registry(workspace: Path) -> Any:
    # Imported here so merely importing this module does not load the tool and permission layers.
    from velune.tools.base.registry import ToolRegistry
    from velune.tools.filesystem.read import ReadFile

    registry = ToolRegistry()
    registry.register(ReadFile(workspace=Path(workspace)))
    return registry


async def run_native_task(
    task: str,
    *,
    config: Any,
    providers: Any,
    provider_id: str,
    model_id: str,
    workspace: Path,
    ask: AskCallable | None,
    prompt_timeout_s: float = DEFAULT_PROMPT_TIMEOUT_S,
    wall_clock_s: float = DEFAULT_WALL_CLOCK_S,
    scrub: Callable[[str], str] | None = None,
) -> NativeRunResult:
    """Run ``task`` as one read-only acting agent. See the module docstring for the guarantees."""
    if not engine_enabled(config):
        raise EngineDisabledError
    provider_id = _clean_id(provider_id, "provider")
    model_id = _clean_id(model_id, "model")
    try:
        spec_input = task_input("run-" + RandomIds().next_id("n"), task)
    except NativeTaskError:
        raise NativeSessionError("the task is empty or too long") from None

    provider = _lookup(providers, provider_id)
    workspace = Path(workspace)
    registry = _read_only_registry(workspace)
    try:
        stack = build_acting_stack(
            provider,
            model_id,
            registry,
            workspace,
            native_prompts(),
            RandomIds(),
            approvals=InteractiveApprovalPort(safe_ask(ask), timeout_s=prompt_timeout_s),
            scrub=scrub,
            invoker_options={"retry": NO_RETRY},
        )
    except Exception:  # includes ScrubberUnavailable: refuse rather than run with weaker redaction
        raise NativeSessionError(
            "the secret scrubber could not be initialised; the run was not started"
        ) from None

    control = RunControl.create(
        spec_input.run_id, ExecutionBudget(wall_clock_s=float(wall_clock_s))
    )
    try:
        outcome = await stack.runner.run_acting(read_only_agent(), spec_input, control=control)
    except asyncio.CancelledError:
        control.token.cancel()  # nothing further may start; the cancellation itself still propagates
        raise

    tools = tuple((record.tool, record.status.value) for record in outcome.tool_records)
    if outcome.ok:
        return NativeRunResult(
            ok=True,
            text=outcome.text,
            message="",
            failure_kind=None,
            turns=outcome.turns,
            tools=tools,
            provider_id=provider_id,
            model_id=model_id,
        )
    failure = outcome.failure
    kind = failure.kind if failure else ActingFailureKind.INTERNAL
    return NativeRunResult(
        ok=False,
        text=None,
        message=(failure.message if failure and failure.message else "the native run failed"),
        failure_kind=str(kind.value),
        turns=outcome.turns,
        tools=tools,
        provider_id=provider_id,
        model_id=model_id,
    )


@dataclass(frozen=True)
class PlanReviewResult:
    """What a plan/review run came to. ``status`` is one of the ``STATUS_*`` values below."""

    ok: bool
    status: str
    answer: str | None
    message: str  # stable text; "" on success
    failure_kind: str | None
    review_rounds: int
    model_calls: int
    provider_attempts: int
    tool_calls: int
    unsettled_calls: int
    provider_id: str
    model_id: str
    same_model_review: bool = True  # planner, executor and reviewer are one provider/model


STATUS_COMPLETED = "completed"
STATUS_DEADLINE = "deadline"
STATUS_BUDGET = "budget_exhausted"
STATUS_DENIED = "denied"
STATUS_FAILED = "failed"

_STATUS_OF_KIND = {
    "deadline": STATUS_DEADLINE,
    "budget_exhausted": STATUS_BUDGET,
    "acting_budget_refused": STATUS_BUDGET,
    "acting_approval_denied": STATUS_DENIED,
}
_MESSAGE_OF_KIND = {
    "deadline": "The time limit was reached before the run finished. No further requests were made.",
    "budget_exhausted": (
        "The run's request budget was used up before it finished. No further requests were made."
    ),
    "acting_budget_refused": "A tool call was refused by the run's budget, so the run stopped.",
    "acting_approval_denied": "A tool call was not approved, so the run stopped.",
    "retry_limit": "The reviewer did not approve the answer within the allowed revisions.",
    "validation_failed": "The answer failed validation.",
    "invalid_output": "The reviewer gave no usable verdict.",
    "tool_outcome_unknown": (
        "A tool call may have run and its outcome is unknown, so the run stopped."
    ),
}


def _failure_text(kind: str, detail: str) -> str:
    fixed = _MESSAGE_OF_KIND.get(kind)
    if fixed:
        return fixed
    # Engine and adapter text only (fixed phrases, never provider or exception text); sanitized anyway.
    return f"The plan/review run did not complete ({sanitize_text(kind, 40)}): " + sanitize_text(
        detail, 200
    )


async def run_native_plan_review(
    task: str,
    *,
    config: Any,
    providers: Any,
    provider_id: str,
    model_id: str,
    workspace: Path,
    ask: AskCallable | None,
    prompt_timeout_s: float = DEFAULT_PROMPT_TIMEOUT_S,
    wall_clock_s: float = DEFAULT_WALL_CLOCK_S,
    scrub: Callable[[str], str] | None = None,
    max_review_attempts: int = DEFAULT_REVIEW_ATTEMPTS,
) -> PlanReviewResult:
    """Run ``task`` through plan, execute (read-only, approval-gated), review and revision.

    Ownership: this function creates the run's ONE :class:`RunControl` (wall clock, model-call and
    provider-attempt budget, cancellation) and hands it to the driver and, through a view, to the
    reasoning invoker; the acting runner receives it from the driver. Nothing else creates one. The
    driver is the only artifact writer and the only state machine owner; this function reads its result.
    """
    if not engine_enabled(config):
        raise EngineDisabledError
    provider_id = _clean_id(provider_id, "provider")
    model_id = _clean_id(model_id, "model")
    try:
        text = clean_task(task)
    except NativeTaskError:
        raise NativeSessionError("the task is empty or too long") from None
    provider = _lookup(providers, provider_id)
    registry = _read_only_registry(workspace)
    ids = RandomIds()
    prompts = plan_review_prompts()
    try:
        stack = build_acting_stack(
            provider,
            model_id,
            registry,
            Path(workspace),
            prompts,
            ids,
            approvals=InteractiveApprovalPort(safe_ask(ask), timeout_s=prompt_timeout_s),
            scrub=scrub,
            invoker_options={"retry": NO_RETRY},
        )
        run_id = "run-" + ids.next_id("n")
        # The one control, created before the first planning request: its clock covers everything.
        control = RunControl.create(run_id, ExecutionBudget(wall_clock_s=float(wall_clock_s)))
        reasoning = ProviderReasoningInvoker(stack.invoker, limits=ControlLimits(control))
        reasoning_runner = ReasoningAgentRunner(
            reasoning, prompts, clock=MonotonicClock(), ids=ids, grace_s=1.0
        )
        driver = create_plan_review_driver(
            config,
            AgentRegistry(plan_review_agents()),
            reasoning_runner,
            ids,
            max_attempts=max_review_attempts,
            acting_runner=stack.runner,
        )
    except EngineDisabledError:
        raise
    except Exception:  # includes ScrubberUnavailable: refuse rather than run with weaker redaction
        raise NativeSessionError(
            "the secret scrubber or the run could not be initialised; the run was not started"
        ) from None

    handle = RunHandle()
    try:
        outcome = await driver.run(text, run_id=run_id, handle=handle, control=control)
    except asyncio.CancelledError:
        control.token.cancel()  # idempotent: the driver has already done so; nothing further starts
        raise

    usage = control.budget.usage()
    state = outcome.state
    common: dict[str, Any] = {
        "review_rounds": state.attempt,
        "model_calls": usage.model_calls,
        "provider_attempts": usage.provider_attempts,
        "tool_calls": usage.calls,
        "unsettled_calls": outcome.unsettled_calls + outcome.abandoned_count,
        "provider_id": provider_id,
        "model_id": model_id,
    }
    if state.status is RunStatus.SUCCEEDED and outcome.answer:
        return PlanReviewResult(
            ok=True,
            status=STATUS_COMPLETED,
            answer=outcome.answer,
            message="",
            failure_kind=None,
            **common,
        )
    failure = state.failure
    kind = failure.kind if failure else "internal"
    return PlanReviewResult(
        ok=False,
        status=_STATUS_OF_KIND.get(kind, STATUS_FAILED),
        answer=None,
        message=_failure_text(kind, failure.message if failure else ""),
        failure_kind=kind,
        **common,
    )


__all__ = [
    "DEFAULT_REVIEW_ATTEMPTS",
    "DEFAULT_WALL_CLOCK_S",
    "STATUS_BUDGET",
    "STATUS_COMPLETED",
    "STATUS_DEADLINE",
    "STATUS_DENIED",
    "STATUS_FAILED",
    "NativeRunResult",
    "PlanReviewResult",
    "NativeSessionError",
    "run_native_plan_review",
    "run_native_task",
    "safe_ask",
    "sanitize_text",
]
