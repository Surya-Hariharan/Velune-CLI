"""The engine's seam to a model, and its prompt source.

``velune.council.ports.SeatInvoker`` is keyed by a deliberation ``StageId`` and a ``SeatKind``, and
its runtime binding only serves seats that belong to a council ``RoleProfile``. An engine agent is
neither, and mapping one onto a council stage would mislabel every trace. So the engine owns this
small port and reuses the council's value types (``SeatMessage``, ``SeatStatus``, ``SeatError``,
``ModelRef``) and the ``Clock`` / ``IdSource`` / ``TraceSink`` ports unchanged. The council core is
not modified; a runtime binding for ``ReasoningInvoker`` is a later phase.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Protocol, runtime_checkable

from pydantic import Field, StringConstraints, model_validator

from velune.core.types.agent import AgentRole
from velune.council.ports import SeatMessage
from velune.council.profiles import RoutingRole
from velune.council.results import ModelRef, SeatError, SeatStatus
from velune.council.serialization import Contract
from velune.orchestration.engine.proposals import ProposedToolCall, ToolNameStr

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]


class ReasoningRequest(Contract):
    """One tool-less completion for one agent. Names a routing role, never a provider or model."""

    run_id: Identifier
    call_id: Identifier
    agent: AgentRole
    routing_role: RoutingRole
    attempt: int = Field(ge=1)
    messages: tuple[SeatMessage, ...] = Field(min_length=1)
    timeout_s: float = Field(gt=0)
    max_tokens: int | None = Field(default=None, gt=0)


class ReasoningResult(Contract):
    """What came back. As with ``SeatResult``, a failure never carries text and never looks ok."""

    status: SeatStatus
    text: str | None = None
    error: SeatError | None = None
    model: ModelRef | None = None
    attempts: int = Field(default=1, ge=0)
    fallback_used: bool = False

    @model_validator(mode="after")
    def _failure_is_not_success(self) -> ReasoningResult:
        if self.status is SeatStatus.OK:
            if self.text is None:
                raise ValueError("an ok result must carry text")
            if self.error is not None:
                raise ValueError("an ok result cannot carry an error")
        else:
            if self.text is not None:
                raise ValueError("a result that did not deliver cannot carry text")
            if self.error is not None and self.error.kind is SeatStatus.OK:
                raise ValueError("an error cannot have status ok")
        return self

    @property
    def ok(self) -> bool:
        return self.status is SeatStatus.OK

    @classmethod
    def success(cls, text: str, **extra: object) -> ReasoningResult:
        return cls(status=SeatStatus.OK, text=text, **extra)  # type: ignore[arg-type]

    @classmethod
    def failure(cls, status: SeatStatus, message: str = "", **extra: object) -> ReasoningResult:
        if status is SeatStatus.OK:
            raise ValueError("failure() needs a non-ok status")
        return cls(
            status=status,
            error=SeatError(kind=status, message=message[:300]),
            **extra,  # type: ignore[arg-type]
        )


@runtime_checkable
class ReasoningInvoker(Protocol):
    """Runs one reasoning request. Never raises: every failure is a typed ``ReasoningResult``.

    The one exception is ``asyncio.CancelledError``, which must propagate so a cancelled run stops.
    """

    async def invoke(self, request: ReasoningRequest) -> ReasoningResult: ...


MAX_PROPOSALS = 16


class ActingRequest(ReasoningRequest):
    """A request to an acting agent. ``offered_tools`` are names only: no schemas, no handles.

    Nothing at this stage sends tool definitions to a provider; the names say what a later layer
    would be allowed to offer, and they come from trusted policy resolution, never from the model.
    """

    offered_tools: tuple[ToolNameStr, ...] = Field(default=(), max_length=64)
    # Set by the trusted runner, never by a model. False once any tool call has been started in this
    # run: from then on a provider call must not be retried (and nothing may switch provider).
    provider_retry_allowed: bool = True
    # Set by the trusted runner. An invoker that sends provider requests calls ``reserve()`` once
    # immediately before EACH request and sends nothing when it returns False. Never serialized.
    attempt_gate: Any = Field(default=None, exclude=True, repr=False)


class ActingResult(Contract):
    """What an acting agent returned: text and/or structured tool-call *proposals*.

    Proposals are untrusted model output. This type has no field for an approval, a result or an
    author, and a failure never carries proposals.
    """

    status: SeatStatus
    text: str | None = None
    proposals: tuple[ProposedToolCall, ...] = Field(default=(), max_length=MAX_PROPOSALS)
    error: SeatError | None = None
    model: ModelRef | None = None
    attempts: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def _failure_is_not_success(self) -> ActingResult:
        if self.status is SeatStatus.OK:
            if self.text is None and not self.proposals:
                raise ValueError("an ok result must carry text or tool-call proposals")
            if self.error is not None:
                raise ValueError("an ok result cannot carry an error")
        else:
            if self.text is not None or self.proposals:
                raise ValueError("a result that did not deliver cannot carry text or proposals")
            if self.error is not None and self.error.kind is SeatStatus.OK:
                raise ValueError("an error cannot have status ok")
        ids = [item.call_id for item in self.proposals]
        if len(set(ids)) != len(ids):
            raise ValueError("proposal call ids must be unique")
        return self

    @property
    def ok(self) -> bool:
        return self.status is SeatStatus.OK

    @classmethod
    def success(
        cls, text: str | None = None, proposals: tuple[ProposedToolCall, ...] = (), **extra: object
    ) -> ActingResult:
        return cls(status=SeatStatus.OK, text=text, proposals=proposals, **extra)  # type: ignore[arg-type]

    @classmethod
    def failure(cls, status: SeatStatus, message: str = "", **extra: object) -> ActingResult:
        if status is SeatStatus.OK:
            raise ValueError("failure() needs a non-ok status")
        return cls(
            status=status,
            error=SeatError(kind=status, message=message[:300]),
            **extra,  # type: ignore[arg-type]
        )


@runtime_checkable
class ActingInvoker(Protocol):
    """Asks an acting agent for its next move. Never executes anything; never raises (but cancels).

    The method is ``propose`` rather than ``invoke`` on purpose: a ``ReasoningInvoker`` does not
    satisfy this protocol and cannot be passed where proposals are expected, and an acting invoker
    cannot be mistaken for a reasoning one.
    """

    async def propose(self, request: ActingRequest) -> ActingResult: ...


@runtime_checkable
class PromptResolver(Protocol):
    """Static system-prompt text for a prompt reference. The engine never holds the wording."""

    def resolve(self, prompt_ref: str) -> str:
        """Return the prompt, or raise ``LookupError`` if the reference is unknown."""
        ...


class StaticPromptResolver:
    """A fixed mapping from prompt reference to text, for wiring and tests."""

    def __init__(self, prompts: Mapping[str, str]) -> None:
        self._prompts = dict(prompts)

    def resolve(self, prompt_ref: str) -> str:
        try:
            return self._prompts[prompt_ref]
        except KeyError:
            raise LookupError(f"no prompt registered for {prompt_ref!r}") from None
