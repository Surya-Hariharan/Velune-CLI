"""The acting runner: a bounded turn loop that turns model proposals into at most one tool call a turn.

This is deliberately *not* ``ReasoningAgentRunner`` (which keeps refusing acting specs) and not a
generic tool loop. It has one narrow job: ask an acting agent for its next move, hand any proposed
call to the trusted executor, show the sanitized result back as framed untrusted data, and repeat
until the agent gives a final draft or a bound is hit.

Rules it enforces (each is covered by a test):

* Everything the model returns is untrusted. Proposals are validated by ``validate_proposals``
  against the policy resolved *here*, from the spec and the trusted catalog, never from anything the
  model said. At most one proposal per turn; more than one rejects the whole response. The runner
  never calls a tool: ``TrustedToolExecutor.execute`` is the only way a call can run, and it rechecks
  policy and the live registry itself.
* Prose that accompanies proposals is dropped. It is never stored, shown to a later turn or returned.
* The request id is built from the caller's ``run_id``/``attempt`` and a runner-owned turn counter,
  never from the model's ``call_id``. The caller's ``RunControl`` is used for every turn and is never
  created or reset here, so budgets and idempotency span the whole run.
* Nothing is retried. Any call that is not a clean success ends the run with a stable failure kind.
  A call that may have run (``executed``) and whose outcome is not settled is reported as
  ``acting_outcome_unknown``, which is distinct from an ordinary tool failure, and is never marked
  safe to retry.
* Cancellation is never swallowed: ``asyncio.CancelledError`` propagates, and a cooperative
  ``RunControl`` cancellation observed at a turn boundary or in a result is raised the same way. A
  tool that was already running is not interrupted; ``RunControl`` keeps its state for the caller.
* The runner holds a small local transcript for the model and returns structured ``ToolRecord``s. It
  never writes to an ``ArtifactStore`` and invents no agent author: storing tool artifacts is the
  driver's job. Tool output is rendered with the Phase 4D-4a tool framing, which is *framing only*.
  The model may still be manipulated by what a tool returned.

Not exported from the engine package: it imports the executor, which reaches the permission and tool
modules, and the package import must stay free of them.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field, StringConstraints, model_validator

from velune._compat import StrEnum
from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind, ReadScope
from velune.council.ports import IdSource, SeatMessage
from velune.council.results import ModelRef, SeatStatus
from velune.council.serialization import Contract
from velune.orchestration.engine.errors import EngineError
from velune.orchestration.engine.execcontrol import RunControl
from velune.orchestration.engine.ports import ActingInvoker, ActingRequest, PromptResolver
from velune.orchestration.engine.runner import (
    AgentInput,
    ArtifactOrigin,
    ContextItem,
    _render_user_message,
)
from velune.orchestration.engine.spec import AgentKind, AgentSpec
from velune.orchestration.engine.toolexec import (
    SCRUB_FAILED_TEXT,
    ToolErrorCategory,
    ToolExecutionResult,
    ToolOutcome,
    ToolStatus,
)
from velune.orchestration.engine.toolpolicy import (
    ResolvedToolPolicy,
    ToolDescriptor,
    ToolPolicyError,
    resolve_tool_policy,
)
from velune.orchestration.engine.toolvalidate import (
    ToolCallRejected,
    ToolRejection,
    ValidatedToolCall,
    validate_proposals,
)

DEFAULT_MAX_TURNS = 8
MAX_TURNS_LIMIT = 32
DEFAULT_TRANSCRIPT_CHARS = 100_000
MIN_TRANSCRIPT_CHARS = 100
MAX_TRANSCRIPT_CHARS = 2_000_000
EMPTY_OUTPUT_TEXT = "[the tool returned no output]"
TRANSCRIPT_CLIPPED_MARKER = "\n[output clipped to fit the transcript]"
_MAX_REQUEST_ID = 64

ShortText = Annotated[str, StringConstraints(max_length=300)]


class ActingConfigError(EngineError, ValueError):
    """The acting runner was built with unusable limits."""


class ActingFailureKind(StrEnum):
    """Stable, value-free failure kinds. Each fits the ``RunFailure.kind`` pattern as is."""

    INVALID_SPEC = "acting_invalid_spec"
    INVALID_POLICY = "acting_invalid_policy"
    INVALID_INPUT = "acting_invalid_input"
    PROVIDER = "acting_provider_error"
    TIMEOUT = "acting_timeout"
    MALFORMED_PROPOSAL = "acting_malformed_proposal"
    MULTIPLE_PROPOSALS = "acting_multiple_proposals"
    POLICY_REJECTED = "acting_policy_rejected"
    APPROVAL_DENIED = "acting_approval_denied"
    BUDGET_REFUSED = "acting_budget_refused"
    MODEL_BUDGET = "acting_model_budget"
    TOOL_FAILED = "acting_tool_failed"
    OUTCOME_UNKNOWN = "acting_outcome_unknown"
    DUPLICATE_REQUEST = "acting_duplicate_request"
    DUPLICATE_DIGEST = "acting_duplicate_digest"
    TURN_LIMIT = "acting_turn_limit"
    TRANSCRIPT_FULL = "acting_transcript_full"
    EMPTY_DRAFT = "acting_empty_draft"
    INTERNAL = "acting_internal"


_MESSAGE: Mapping[ActingFailureKind, str] = {
    ActingFailureKind.INVALID_SPEC: "the agent is not an acting agent",
    ActingFailureKind.INVALID_POLICY: "the agent's tool policy could not be resolved",
    ActingFailureKind.INVALID_INPUT: "the acting input is unusable",
    ActingFailureKind.PROVIDER: "the model call did not deliver",
    ActingFailureKind.TIMEOUT: "the model call timed out",
    ActingFailureKind.MALFORMED_PROPOSAL: "a tool-call proposal failed validation",
    ActingFailureKind.MULTIPLE_PROPOSALS: "more than one tool call was proposed in one turn",
    ActingFailureKind.POLICY_REJECTED: "a tool call was refused by policy or authorization",
    ActingFailureKind.APPROVAL_DENIED: "a tool call was not approved",
    ActingFailureKind.BUDGET_REFUSED: "a tool call was refused by the run's budget",
    ActingFailureKind.MODEL_BUDGET: "the run's model-call budget or deadline is exhausted",
    ActingFailureKind.TOOL_FAILED: "a tool call failed",
    ActingFailureKind.OUTCOME_UNKNOWN: "a tool call may have run and its outcome is unknown",
    ActingFailureKind.DUPLICATE_REQUEST: "a tool request id was already used in this run",
    ActingFailureKind.DUPLICATE_DIGEST: "a tool call matching an unsettled call was not run again",
    ActingFailureKind.TURN_LIMIT: "the agent did not finish within the turn limit",
    ActingFailureKind.TRANSCRIPT_FULL: "the tool results no longer fit the input limit",
    ActingFailureKind.EMPTY_DRAFT: "the agent returned an empty final draft",
    ActingFailureKind.INTERNAL: "an internal error ended the acting run",
}

_APPROVAL = frozenset(
    {
        ToolErrorCategory.APPROVAL_MISSING,
        ToolErrorCategory.APPROVAL_DENIED,
        ToolErrorCategory.APPROVAL_STALE,
        ToolErrorCategory.APPROVAL_MISMATCH,
    }
)
_BUDGET = frozenset(
    {
        ToolErrorCategory.BUDGET_DEADLINE,
        ToolErrorCategory.BUDGET_CALLS,
        ToolErrorCategory.BUDGET_ARGUMENT_BYTES,
        ToolErrorCategory.BUDGET_RESULT_BYTES,
        ToolErrorCategory.BUDGET_CONCURRENCY,
    }
)
_POLICY = frozenset(
    {
        ToolErrorCategory.MALFORMED,
        ToolErrorCategory.NOT_ALLOWED,
        ToolErrorCategory.UNKNOWN_TOOL,
        ToolErrorCategory.TOOL_CHANGED,
        ToolErrorCategory.AUTHORIZATION_DENIED,
        ToolErrorCategory.HOOK_BLOCKED,
    }
)
_UNSETTLED = frozenset({ToolOutcome.OUTCOME_UNKNOWN, ToolOutcome.CANCEL_REQUESTED_DURING_EXECUTION})
# Rejections that mean "not this agent's to use", as opposed to "badly formed".
_POLICY_REJECTIONS = frozenset({ToolRejection.NOT_ALLOWED, ToolRejection.UNKNOWN_TOOL})


class ToolRecord(Contract):
    """Trusted facts about one tool call the runner made, for the driver to store and to check later.

    ``output`` is untrusted tool data (already redacted and clipped by the executor); everything else
    is engine-produced. ``unsettled`` is true when the call may have run and its outcome is not known;
    the driver must not run the same ``call_digest`` again while one is unsettled.
    """

    turn: int = Field(ge=1)
    request_id: str
    tool: str
    call_digest: str
    status: ToolStatus
    outcome: ToolOutcome
    error: ToolErrorCategory | None = None
    executed: bool = False
    cancel_requested: bool = False
    safe_to_retry: bool = False
    unsettled: bool = False
    output: str = ""
    truncated: bool = False
    origin: Literal[ArtifactOrigin.TOOL] = ArtifactOrigin.TOOL


class ActingFailure(Contract):
    kind: ActingFailureKind
    message: ShortText = ""


class ActingStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"


class ActingOutcome(Contract):
    """The result of one acting run. ``tool_records`` are present on success *and* failure."""

    role: AgentRole
    status: ActingStatus
    text: str | None = None
    failure: ActingFailure | None = None
    tool_records: tuple[ToolRecord, ...] = ()
    turns: int = Field(default=0, ge=0)
    model: ModelRef | None = None

    @model_validator(mode="after")
    def _failure_is_not_success(self) -> ActingOutcome:
        if self.status is ActingStatus.OK:
            if not self.text:
                raise ValueError("an ok outcome must carry a final draft")
            if self.failure is not None:
                raise ValueError("an ok outcome cannot carry a failure")
        else:
            if self.text is not None:
                raise ValueError("a failed outcome cannot carry text")
            if self.failure is None:
                raise ValueError("a failed outcome must say why")
        return self

    @property
    def ok(self) -> bool:
        return self.status is ActingStatus.OK

    @property
    def has_unsettled_call(self) -> bool:
        return any(record.unsettled for record in self.tool_records)


@runtime_checkable
class ToolExecutor(Protocol):
    """The one narrow capability the runner needs: run a validated call under a run's control."""

    async def execute(
        self,
        call: ValidatedToolCall,
        policy: ResolvedToolPolicy,
        *,
        run_id: str,
        control: RunControl | None = None,
        request_id: str | None = None,
    ) -> ToolExecutionResult: ...


@runtime_checkable
class ActingRunner(Protocol):
    """Runs one acting agent to a final draft or a failure. Raises only ``CancelledError``."""

    async def run_acting(
        self,
        spec: AgentSpec,
        input: AgentInput,
        *,
        control: RunControl,
        blocked_digests: frozenset[str] = frozenset(),
    ) -> ActingOutcome: ...


class _Progress:
    """What happened so far, kept outside the loop so a late failure still reports it."""

    def __init__(self) -> None:
        self.turns = 0
        self.records: list[ToolRecord] = []
        self.model: ModelRef | None = None
        self.transcript: list[ContextItem] = []
        self.transcript_chars = 0


class ActingAgentRunner:
    """``ActingRunner`` over an :class:`ActingInvoker` and a :class:`ToolExecutor`."""

    def __init__(
        self,
        invoker: ActingInvoker,
        executor: ToolExecutor,
        prompts: PromptResolver,
        catalog: Mapping[str, ToolDescriptor],
        *,
        ids: IdSource,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_transcript_chars: int = DEFAULT_TRANSCRIPT_CHARS,
        grace_s: float = 1.0,
        scrub: Callable[[str], str] | None = None,
    ) -> None:
        if isinstance(max_turns, bool) or not 1 <= max_turns <= MAX_TURNS_LIMIT:
            raise ActingConfigError(f"max_turns must be between 1 and {MAX_TURNS_LIMIT}")
        if (
            isinstance(max_transcript_chars, bool)
            or not MIN_TRANSCRIPT_CHARS <= max_transcript_chars <= MAX_TRANSCRIPT_CHARS
        ):
            raise ActingConfigError(
                "max_transcript_chars must be between "
                f"{MIN_TRANSCRIPT_CHARS} and {MAX_TRANSCRIPT_CHARS}"
            )
        self._invoker = invoker
        self._executor = executor
        self._prompts = prompts
        self._catalog = dict(catalog)  # a snapshot: later changes cannot widen what is offered
        self._ids = ids
        self._max_turns = max_turns
        self._max_transcript_chars = max_transcript_chars
        self._grace_s = max(0.0, grace_s)
        # Defence in depth: tool output and the final draft pass through this before they can enter a
        # later prompt, a ToolRecord or a returned outcome. The executor scrubs before it clips; this
        # catches an executor (or invoker) that did not.
        self._scrub = scrub

    def _scrubbed(self, text: str) -> str:
        if self._scrub is None or not text:
            return text
        try:
            return self._scrub(text)
        except Exception:
            return SCRUB_FAILED_TEXT

    async def run_acting(
        self,
        spec: AgentSpec,
        input: AgentInput,
        *,
        control: RunControl,
        blocked_digests: frozenset[str] = frozenset(),
    ) -> ActingOutcome:
        """Run to a final draft or a failure.

        ``blocked_digests`` is trusted, driver-supplied: a call whose ``call_digest`` is listed is
        refused before it reaches the executor (an earlier identical call has not settled).
        """
        progress = _Progress()
        try:
            return await self._run(spec, input, control, progress, blocked_digests)
        except asyncio.CancelledError as exc:
            # Cancellation still propagates and still wins. The records are attached so the caller
            # keeps what is known, including a call that is confirmed complete: its result is not
            # lost, and it is not reclassified as unknown because cancellation arrived around it.
            exc.tool_records = tuple(progress.records)  # type: ignore[attr-defined]
            raise
        except Exception:  # a runner defect: end this run, report only what is known
            return self._failed(spec, ActingFailureKind.INTERNAL, progress)

    # ── the loop ──────────────────────────────────────────────────────────────

    async def _run(
        self,
        spec: AgentSpec,
        input: AgentInput,
        control: RunControl,
        progress: _Progress,
        blocked_digests: frozenset[str],
    ) -> ActingOutcome:
        if spec.kind is not AgentKind.ACTING:
            return self._failed(spec, ActingFailureKind.INVALID_SPEC, progress)
        if control.run_id != input.run_id:
            return self._failed(spec, ActingFailureKind.INVALID_INPUT, progress)
        violation = _scope_violation(spec, input)
        if violation:
            return self._failed(spec, ActingFailureKind.INVALID_INPUT, progress)
        try:
            policy = resolve_tool_policy(spec, self._catalog)
        except ToolPolicyError:
            return self._failed(spec, ActingFailureKind.INVALID_POLICY, progress)
        if not policy.allowed:
            return self._failed(spec, ActingFailureKind.INVALID_POLICY, progress)
        try:
            system = self._prompts.resolve(spec.prompt_ref).strip()
        except LookupError:
            system = ""
        if not system:
            return self._failed(spec, ActingFailureKind.INVALID_INPUT, progress)
        offered = tuple(sorted(policy.allowed))

        for turn in range(1, self._max_turns + 1):
            if control.token.cancelled:
                raise asyncio.CancelledError
            user = _render_user_message(
                input.model_copy(update={"context": (*input.context, *progress.transcript)})
            )
            if len(system) + len(user) > spec.budget.max_input_chars:
                kind = (
                    ActingFailureKind.INVALID_INPUT
                    if turn == 1
                    else ActingFailureKind.TRANSCRIPT_FULL
                )
                return self._failed(spec, kind, progress)

            refusal = control.budget.reserve_model_call()
            if refusal is not None:  # fail closed: no provider request is made
                return self._failed(spec, ActingFailureKind.MODEL_BUDGET, progress)
            progress.turns = turn
            request = ActingRequest(
                run_id=input.run_id,
                call_id=self._ids.next_id("call"),
                agent=spec.role,
                routing_role=spec.routing_role,
                attempt=input.attempt,
                messages=(
                    SeatMessage(role="system", content=system),
                    SeatMessage(role="user", content=user),
                ),
                timeout_s=spec.budget.timeout_s,
                max_tokens=spec.budget.max_output_tokens,
                offered_tools=offered,
                # Once a tool call has been started anywhere in this run, a failed provider call is
                # not retried: the run ends instead.
                provider_retry_allowed=not (progress.records or control.budget.usage().calls > 0),
            )
            try:
                result = await asyncio.wait_for(
                    self._invoker.propose(request), timeout=spec.budget.timeout_s + self._grace_s
                )
            except asyncio.TimeoutError:
                return self._failed(spec, ActingFailureKind.TIMEOUT, progress)
            except asyncio.CancelledError:
                raise
            except Exception:  # the invoker broke its never-raises contract
                return self._failed(spec, ActingFailureKind.INTERNAL, progress)

            control.budget.note_provider_attempts(result.attempts)
            if result.status is SeatStatus.CANCELLED or control.token.cancelled:
                # Never an ordinary, retryable failure, and nothing further starts: no validation
                # that could lead to a tool call, and no tool call.
                raise asyncio.CancelledError
            if not result.ok:
                return self._failed(
                    spec,
                    ActingFailureKind.PROVIDER,
                    progress,
                    detail=f"the model call ended as {result.status.value}",
                )
            progress.model = result.model or progress.model

            if not result.proposals:
                draft = self._scrubbed((result.text or "").strip()).strip()
                if not draft:
                    return self._failed(spec, ActingFailureKind.EMPTY_DRAFT, progress)
                return ActingOutcome(
                    role=spec.role,
                    status=ActingStatus.OK,
                    text=draft,
                    tool_records=tuple(progress.records),
                    turns=turn,
                    model=progress.model,
                )

            # A continuation: ``result.text`` is dropped here and never used again.
            if len(result.proposals) > 1:
                return self._failed(spec, ActingFailureKind.MULTIPLE_PROPOSALS, progress)
            try:
                (call,) = validate_proposals(result.proposals, policy, self._catalog)
            except ToolCallRejected as rejected:
                kind = (
                    ActingFailureKind.POLICY_REJECTED
                    if rejected.kind in _POLICY_REJECTIONS
                    else ActingFailureKind.MALFORMED_PROPOSAL
                )
                return self._failed(spec, kind, progress)

            if call.call_digest in blocked_digests:
                return self._failed(spec, ActingFailureKind.DUPLICATE_DIGEST, progress)
            failure = await self._execute(call, policy, input, control, progress, turn)
            if failure is not None:
                return self._failed(spec, failure, progress)
            if not self._remember(call.name, progress):
                return self._failed(spec, ActingFailureKind.TRANSCRIPT_FULL, progress)

        return self._failed(spec, ActingFailureKind.TURN_LIMIT, progress)

    async def _execute(
        self,
        call: ValidatedToolCall,
        policy: ResolvedToolPolicy,
        input: AgentInput,
        control: RunControl,
        progress: _Progress,
        turn: int,
    ) -> ActingFailureKind | None:
        """Run one validated call. Returns a failure kind, or ``None`` after recording a clean success."""
        request_id = _request_id(input.run_id, input.attempt, turn)
        try:
            result = await self._executor.execute(
                call, policy, run_id=input.run_id, control=control, request_id=request_id
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The executor never raises by contract. If it did, the call may have started.
            progress.records.append(_unknown_record(call, turn, request_id))
            return ActingFailureKind.OUTCOME_UNKNOWN

        record = _record(result, turn, request_id)
        if record.output:
            record = record.model_copy(update={"output": self._scrubbed(record.output)})
        progress.records.append(record)
        if result.cancel_requested or result.error is ToolErrorCategory.CANCELLED:
            raise asyncio.CancelledError  # cooperative cancellation wins; control keeps the state
        return _failure_for(result)

    def _remember(self, tool: str, progress: _Progress) -> bool:
        """Append the last record to the local transcript, clipped to the cap. False if no room."""
        record = progress.records[-1]
        text = record.output or EMPTY_OUTPUT_TEXT
        room = self._max_transcript_chars - progress.transcript_chars
        if room <= 0:
            return False
        if len(text) > room:
            keep = max(0, room - len(TRANSCRIPT_CLIPPED_MARKER))
            text = (text[:keep] + TRANSCRIPT_CLIPPED_MARKER)[:room]
        progress.transcript_chars += len(text)
        progress.transcript.append(
            ContextItem(
                kind=ArtifactKind.EVIDENCE,
                text=text,
                origin=ArtifactOrigin.TOOL,  # stamped here, from the executor's own result
                source=tool,
            )
        )
        return True

    @staticmethod
    def _failed(
        spec: AgentSpec, kind: ActingFailureKind, progress: _Progress, *, detail: str = ""
    ) -> ActingOutcome:
        return ActingOutcome(
            role=spec.role,
            status=ActingStatus.FAILED,
            failure=ActingFailure(kind=kind, message=(detail or _MESSAGE[kind])[:300]),
            tool_records=tuple(progress.records),
            turns=progress.turns,
            model=progress.model,
        )


# ── helpers ───────────────────────────────────────────────────────────────────


def _request_id(run_id: str, attempt: int, turn: int) -> str:
    """``{run_id}.a{attempt}.t{turn}``, with the run id replaced by a digest prefix if too long."""
    candidate = f"{run_id}.a{attempt}.t{turn}"
    if len(candidate) <= _MAX_REQUEST_ID:
        return candidate
    short = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24]
    return f"{short}.a{attempt}.t{turn}"


def _scope_violation(spec: AgentSpec, input: AgentInput) -> str:
    for item in input.context:
        scope = spec.scope_for(item.kind)
        if scope is ReadScope.NONE:
            return f"{spec.role.value} may not read {item.kind.value} artifacts"
        if scope is ReadScope.OWN and item.author is not spec.role:
            return f"{spec.role.value} may read only its own {item.kind.value} artifacts"
    return ""


def _record(result: ToolExecutionResult, turn: int, request_id: str) -> ToolRecord:
    return ToolRecord(
        turn=turn,
        request_id=request_id,
        tool=result.tool,
        call_digest=result.call_digest,
        status=result.status,
        outcome=result.outcome,
        error=result.error,
        executed=result.executed,
        cancel_requested=result.cancel_requested,
        # The runner never upgrades this: a call that may have run is never safe to retry.
        safe_to_retry=result.safe_to_retry and not result.executed,
        unsettled=result.executed and result.outcome in _UNSETTLED,
        output=result.output,
        truncated=result.truncated,
    )


def _unknown_record(call: ValidatedToolCall, turn: int, request_id: str) -> ToolRecord:
    return ToolRecord(
        turn=turn,
        request_id=request_id,
        tool=call.name,
        call_digest=call.call_digest,
        status=ToolStatus.FAILED,
        outcome=ToolOutcome.OUTCOME_UNKNOWN,
        error=ToolErrorCategory.OUTCOME_UNKNOWN,
        executed=True,
        unsettled=True,
    )


def _failure_for(result: ToolExecutionResult) -> ActingFailureKind | None:
    """``None`` only for a clean, first-time success. Everything else is a stable failure kind."""
    error, outcome = result.error, result.outcome
    if result.replayed or error is ToolErrorCategory.IDEMPOTENCY_CONFLICT:
        return ActingFailureKind.DUPLICATE_REQUEST
    if (
        outcome in _UNSETTLED
        or error is ToolErrorCategory.OUTCOME_UNKNOWN
        or error is ToolErrorCategory.DUPLICATE_PENDING
    ):
        return ActingFailureKind.OUTCOME_UNKNOWN
    if result.status is ToolStatus.OK and outcome is ToolOutcome.COMPLETED:
        return None
    if error in _APPROVAL:
        return ActingFailureKind.APPROVAL_DENIED
    if error in _BUDGET:
        return ActingFailureKind.BUDGET_REFUSED
    if error in _POLICY:
        return ActingFailureKind.POLICY_REJECTED
    if outcome is ToolOutcome.DEADLINE_EXCEEDED or error in (
        ToolErrorCategory.TOOL_ERROR,
        ToolErrorCategory.DEADLINE_EXCEEDED,
    ):
        return ActingFailureKind.TOOL_FAILED
    # Anything unrecognised: if it may have run we cannot call it an ordinary failure.
    return ActingFailureKind.OUTCOME_UNKNOWN if result.executed else ActingFailureKind.INTERNAL


__all__ = [
    "ActingAgentRunner",
    "ActingConfigError",
    "ActingFailure",
    "ActingFailureKind",
    "ActingOutcome",
    "ActingRunner",
    "ActingStatus",
    "ToolExecutor",
    "ToolRecord",
]
