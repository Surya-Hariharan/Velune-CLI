"""Running one agent: the typed input and outcome, the runner protocol and the reasoning runner.

The contract mirrors ``SeatInvoker``: ``run`` returns an :class:`AgentOutcome` for every ordinary
failure (timeout, provider error, blocked content, bad input, an acting agent we cannot run yet) and
raises only ``asyncio.CancelledError``, which must reach the caller so a cancelled run stops. The
reasoning runner makes exactly one tool-less call; it executes nothing and edits nothing.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, ClassVar, Protocol, runtime_checkable

from pydantic import Field, StringConstraints, model_validator

from velune._compat import StrEnum
from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind, ReadScope
from velune.council.ports import Clock, ContentScreen, IdSource, SeatMessage, TraceSink
from velune.council.results import ModelRef, SeatStatus
from velune.council.serialization import Contract
from velune.council.trace import CouncilTraceEvent, TraceEventKind
from velune.orchestration.engine.ports import (
    Identifier,
    PromptResolver,
    ReasoningInvoker,
    ReasoningRequest,
)
from velune.orchestration.engine.spec import AgentKind, AgentSpec

ShortText = Annotated[str, StringConstraints(max_length=300)]


class FailureKind(StrEnum):
    TIMEOUT = "timeout"
    PROVIDER_ERROR = "provider_error"
    AUTH_ERROR = "auth_error"
    EMPTY = "empty"
    UNPARSEABLE = "unparseable"
    BLOCKED = "blocked"
    UNSUPPORTED = "unsupported"
    INVALID_INPUT = "invalid_input"
    SCOPE_VIOLATION = "scope_violation"
    INTERNAL = "internal"


class OutcomeStatus(StrEnum):
    OK = "ok"
    FAILED = "failed"


class AgentFailure(Contract):
    """Why an agent did not deliver. A stable kind and a short message; never prompt or completion text."""

    kind: FailureKind
    message: ShortText = ""


class ContextItem(Contract):
    """One piece of material shown to the agent, labelled with the kind of artifact it is."""

    kind: ArtifactKind
    text: str = Field(min_length=1)
    author: AgentRole | None = None


class AgentInput(Contract):
    run_id: Identifier
    attempt: int = Field(default=1, ge=1)
    instruction: str = Field(min_length=1)
    context: tuple[ContextItem, ...] = ()


class AgentOutcome(Contract):
    """The result of one agent run. A failure carries no text and an ok outcome carries no failure."""

    role: AgentRole
    call_id: Identifier
    status: OutcomeStatus
    text: str | None = None
    failure: AgentFailure | None = None
    model: ModelRef | None = None
    elapsed_ms: int = Field(default=0, ge=0)

    volatile_fields: ClassVar[frozenset[str]] = frozenset({"elapsed_ms"})

    @model_validator(mode="after")
    def _failure_is_not_success(self) -> AgentOutcome:
        if self.status is OutcomeStatus.OK:
            if not self.text:
                raise ValueError("an ok outcome must carry text")
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
        return self.status is OutcomeStatus.OK


@runtime_checkable
class AgentRunner(Protocol):
    """Runs one agent once. Returns an outcome for every failure except cancellation."""

    async def run(self, spec: AgentSpec, input: AgentInput) -> AgentOutcome: ...


_FAILURE_OF_SEAT_STATUS: dict[SeatStatus, FailureKind] = {
    SeatStatus.TIMEOUT: FailureKind.TIMEOUT,
    SeatStatus.PROVIDER_ERROR: FailureKind.PROVIDER_ERROR,
    SeatStatus.AUTH_ERROR: FailureKind.AUTH_ERROR,
    SeatStatus.EMPTY: FailureKind.EMPTY,
    SeatStatus.UNPARSEABLE: FailureKind.UNPARSEABLE,
    SeatStatus.BLOCKED: FailureKind.BLOCKED,
    SeatStatus.SKIPPED: FailureKind.UNSUPPORTED,
}


class ReasoningAgentRunner:
    """``AgentRunner`` for reasoning agents, over a :class:`ReasoningInvoker`.

    ``grace_s`` is how much longer than ``spec.budget.timeout_s`` the runner waits before giving up
    on an invoker that ignored its own timeout; the invoker's typed ``timeout`` result wins when it
    arrives first.
    """

    def __init__(
        self,
        invoker: ReasoningInvoker,
        prompts: PromptResolver,
        *,
        clock: Clock,
        ids: IdSource,
        trace: TraceSink | None = None,
        screen: ContentScreen | None = None,
        grace_s: float = 1.0,
    ) -> None:
        self._invoker = invoker
        self._prompts = prompts
        self._clock = clock
        self._ids = ids
        self._trace = trace
        self._screen = screen
        self._grace_s = max(0.0, grace_s)
        self._seq = 0

    async def run(self, spec: AgentSpec, input: AgentInput) -> AgentOutcome:
        call_id = self._ids.next_id("call")
        started = self._clock.monotonic()
        try:
            outcome = await self._run(spec, input, call_id)
        except asyncio.CancelledError:
            self._emit(spec, TraceEventKind.CANCELLED, "cancelled", call_id)
            raise
        outcome = outcome.model_copy(update={"elapsed_ms": self._elapsed_ms(started)})
        status = "ok" if outcome.ok else outcome.failure.kind.value  # type: ignore[union-attr]
        self._emit(spec, TraceEventKind.SEAT_RESULT, status, call_id)
        return outcome

    async def _run(self, spec: AgentSpec, input: AgentInput, call_id: str) -> AgentOutcome:
        def failed(kind: FailureKind, message: str) -> AgentOutcome:
            return AgentOutcome(
                role=spec.role,
                call_id=call_id,
                status=OutcomeStatus.FAILED,
                failure=AgentFailure(kind=kind, message=message[:300]),
            )

        if spec.kind is not AgentKind.REASONING:
            return failed(
                FailureKind.UNSUPPORTED,
                "acting agents cannot run yet; this runner executes reasoning agents only",
            )

        violation = self._scope_violation(spec, input)
        if violation:
            return failed(FailureKind.SCOPE_VIOLATION, violation)

        try:
            system = self._prompts.resolve(spec.prompt_ref).strip()
        except LookupError:
            return failed(FailureKind.INVALID_INPUT, f"unknown prompt {spec.prompt_ref!r}")
        if not system:
            return failed(FailureKind.INVALID_INPUT, f"prompt {spec.prompt_ref!r} is empty")

        user = _render_user_message(input)
        if len(system) + len(user) > spec.budget.max_input_chars:
            return failed(
                FailureKind.INVALID_INPUT,
                f"input exceeds the agent's budget of {spec.budget.max_input_chars} characters",
            )
        if self._screen is not None and not self._screen.allows(user):
            return failed(FailureKind.BLOCKED, "the content screen refused the input")

        request = ReasoningRequest(
            run_id=input.run_id,
            call_id=call_id,
            agent=spec.role,
            routing_role=spec.routing_role,
            attempt=input.attempt,
            messages=(
                SeatMessage(role="system", content=system),
                SeatMessage(role="user", content=user),
            ),
            timeout_s=spec.budget.timeout_s,
            max_tokens=spec.budget.max_output_tokens,
        )
        try:
            result = await asyncio.wait_for(
                self._invoker.invoke(request), timeout=spec.budget.timeout_s + self._grace_s
            )
        except asyncio.TimeoutError:
            return failed(FailureKind.TIMEOUT, f"no answer within {spec.budget.timeout_s:.0f}s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the invoker broke its never-raises contract
            return failed(FailureKind.INTERNAL, f"invoker raised {type(exc).__name__}")

        if result.status is SeatStatus.CANCELLED:
            # Cancellation is never an ordinary, retryable failure: surface it as cancellation.
            raise asyncio.CancelledError
        if not result.ok:
            kind = _FAILURE_OF_SEAT_STATUS.get(result.status, FailureKind.PROVIDER_ERROR)
            detail = result.error.message if result.error else ""
            return failed(kind, detail or result.status.value)
        text = (result.text or "").strip()
        if not text:
            return failed(FailureKind.EMPTY, "the model returned no usable text")
        return AgentOutcome(
            role=spec.role,
            call_id=call_id,
            status=OutcomeStatus.OK,
            text=text,
            model=result.model,
        )

    @staticmethod
    def _scope_violation(spec: AgentSpec, input: AgentInput) -> str:
        for item in input.context:
            scope = spec.scope_for(item.kind)
            if scope is ReadScope.NONE:
                return f"{spec.role.value} may not read {item.kind.value} artifacts"
            if scope is ReadScope.OWN and item.author is not spec.role:
                return f"{spec.role.value} may read only its own {item.kind.value} artifacts"
        return ""

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._clock.monotonic() - started) * 1000))

    def _emit(self, spec: AgentSpec, event: TraceEventKind, status: str, call_id: str) -> None:
        if self._trace is None:
            return
        item = CouncilTraceEvent(
            seq=self._seq,
            event=event,
            seat=spec.role.value,
            status=status,
            detail=call_id,
        )
        self._seq += 1
        try:
            self._trace.emit(item)
        except Exception:  # a broken sink must never break a run
            self._trace = None


def _render_user_message(input: AgentInput) -> str:
    parts = [input.instruction.strip()]
    for item in input.context:
        origin = f" from {item.author.value}" if item.author is not None else ""
        parts.append(f"[{item.kind.value}{origin}]\n{item.text.strip()}")
    return "\n\n".join(parts)
