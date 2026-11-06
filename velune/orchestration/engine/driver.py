"""A bounded, reasoning-only plan-and-review driver.

One run walks the Phase 4B state machine, phase by phase, and every phase has a fixed role:

=========  ===========================  ==========================================  ==============
phase      who acts                     reads                                       produces
=========  ===========================  ==========================================  ==============
PLAN       planner agent                the task (QUESTION)                         FRAME (plan)
EXECUTE    reasoner agent               task, plan, own drafts, addressed critique  PERSPECTIVE
OBSERVE    the driver, no model         checks this attempt's draft exists          nothing
REFLECT    reviewer agent               task, plan, drafts by assigned authors      CRITIQUE
RETRY      the driver                   (REFLECT said REVISE)                       attempt + 1
VALIDATE   the driver's validator       the draft the reviewer approved             nothing
FINISH     the driver                   nothing                                     the answer
=========  ===========================  ==========================================  ==============

The driver is the authorization boundary. Context comes only from the :class:`ArtifactStore`
through the :class:`ContextBuilder`; the driver stamps every artifact's kind and author itself;
the instruction handed to an agent is one of the constants below and never contains task or model
text. All transitions go through the machine (``advance``, ``fail``, ``cancel``,
``record_artifact``): this module has no transition logic of its own.

Failure is deterministic. A failed agent call, a review with no verdict, a failed validation and an
exhausted retry budget each end the run as FAILED with a stable ``kind``; nothing is retried or
replanned automatically. The only retry is the reviewer asking for a revision, bounded by
``max_attempts``. Cancellation propagates as ``asyncio.CancelledError`` after the run has been moved
to CANCELLED, which is readable from the :class:`RunHandle` the caller passed in.

Acting (Phase 4D-4c). When an acting runner is supplied the reasoner may be an *acting* agent, and
only EXECUTE changes: the driver hands the runner the run's single :class:`RunControl`, then stores
what came back. The runner never writes to the store. The driver stamps every tool artifact itself:
kind ``EVIDENCE``, origin ``TOOL``, no agent author, the trusted tool name as ``source``. Planner and
reviewer never receive tool-origin content (they have no grant, and the driver filters it as well).
A tool call that may have run and has not settled is tracked by its ``call_digest`` for the life of
the run, is never executed again under a fresh request id, and blocks OBSERVE, RETRY, VALIDATE and
FINISH. No call is retried automatically and nothing here claims a tool was stopped.

Nothing here calls a provider, executes a tool itself, reads the CLI, or persists anything.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import Field

from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind, ReadScope
from velune.council.ports import IdSource
from velune.council.profiles import RoutingRole
from velune.council.serialization import Contract
from velune.orchestration.engine.artifacts import (
    ArtifactStore,
    AssignmentPolicy,
    ContextBuilder,
    StaticAssignments,
    StoredArtifact,
    ToolArtifactMeta,
)
from velune.orchestration.engine.errors import DriverConfigError, RetryLimitExceeded
from velune.orchestration.engine.execcontrol import ExecutionBudget, InvocationState, RunControl
from velune.orchestration.engine.machine import advance, cancel, fail, record_artifact
from velune.orchestration.engine.registry import AgentRegistry
from velune.orchestration.engine.runner import (
    AgentInput,
    AgentKind,
    AgentRunner,
    ArtifactOrigin,
    ContextItem,
)
from velune.orchestration.engine.spec import AgentSpec, ReadGrant, ToolPolicy
from velune.orchestration.state.run_state import (
    MAX_ATTEMPTS_LIMIT,
    RunArtifact,
    RunPhase,
    RunState,
)

PLAN_PROMPT = "engine.plan"
EXECUTE_PROMPT = "engine.execute"
REVIEW_PROMPT = "engine.review"

# The roles the driver runs, and who reads whose work by default.
PLANNER = AgentRole.PLANNER
REASONER = AgentRole.REASONER
REVIEWER = AgentRole.REVIEWER


class Step(Enum):
    PLAN = "plan"
    EXECUTE = "execute"
    REVIEW = "review"


# Driver-authored, fixed text. Task text and model output never reach an instruction.
INSTRUCTIONS: Mapping[Step, str] = MappingProxyType(
    {
        Step.PLAN: "Write a short, numbered plan for the task shown below.",
        Step.EXECUTE: (
            "Carry out the plan for the task shown below and write the answer. "
            "If a critique addressed to you is shown, revise your earlier draft to address it."
        ),
        Step.REVIEW: (
            "Review the latest draft answer shown below against the task and the plan. "
            "Begin your reply with exactly one line, either 'VERDICT: APPROVE' or "
            "'VERDICT: REVISE', then explain briefly."
        ),
    }
)

MAX_EVIDENCE_CHARS = 100_000
EMPTY_EVIDENCE_TEXT = "[the tool returned no output]"
EVIDENCE_CLIPPED_MARKER = "\n[output clipped by the driver]"
UNSETTLED_KIND = "tool_outcome_unknown"
_SETTLED_STATES = frozenset(
    {InvocationState.COMPLETED, InvocationState.FAILED, InvocationState.CANCELLED}
)

_VERDICT = re.compile(r"^\s*VERDICT:\s*(APPROVE|REVISE)\b", re.IGNORECASE)


class Verdict(Enum):
    APPROVE = "approve"
    REVISE = "revise"


def parse_verdict(text: str) -> Verdict | None:
    """The reviewer's verdict from the first non-empty line, or ``None`` if it gave none."""
    for line in text.splitlines():
        if line.strip():
            match = _VERDICT.match(line)
            return Verdict(match.group(1).lower()) if match else None
    return None


Validator = Callable[[str], "str | None"]
"""Returns a reason the answer is unacceptable, or ``None`` if it passes."""


def non_blank(answer: str) -> str | None:
    return None if answer.strip() else "the answer is empty"


class ActingPort(Protocol):
    """What the driver needs from an acting runner (see ``acting.ActingRunner``).

    Declared here, not imported, so importing the driver never loads the executor, the tools or the
    permission system. It returns an ``ActingOutcome``; the driver reads it by attribute only.
    """

    async def run_acting(
        self,
        spec: AgentSpec,
        input: AgentInput,
        *,
        control: RunControl,
        blocked_digests: frozenset[str] = frozenset(),
    ) -> Any: ...


class RunHandle:
    """Where a caller can read the run's latest state, including after a cancellation.

    ``abandoned_count`` and ``unsettled_calls`` are content-free counts: tool calls the executor
    stopped waiting on (they were not interrupted and may still be running), and calls that may have
    run whose outcome is not settled. Neither is ever evidence that a tool was stopped.
    """

    def __init__(self) -> None:
        self.state: RunState | None = None
        self.abandoned_count = 0
        self.unsettled_calls = 0
        self._context: _RunContext | None = None


class DriverResult(Contract):
    state: RunState
    answer: str | None = Field(default=None)
    abandoned_count: int = Field(default=0, ge=0)
    unsettled_calls: int = Field(default=0, ge=0)


class _RunContext:
    """Per-run mutable state, owned by one ``run`` call. The driver itself stays stateless."""

    def __init__(self) -> None:
        self.control: RunControl | None = None
        # call_digest -> request_id of a call that may have run and is not confirmed settled.
        self.unsettled: dict[str, str] = {}


def plan_review_specs(
    acting_tools: tuple[str, ...] = (),
) -> tuple[AgentSpec, AgentSpec, AgentSpec]:
    """The planner, reasoner and reviewer specs, each with the narrowest grants.

    By default all three are reasoning-only. ``acting_tools`` makes only the reasoner an *acting*
    agent allowed those tools (still subject to the trusted catalog and policy resolver), and gives
    it the one extra read grant it needs: the tool evidence. Planner and reviewer never get tools or
    that grant.
    """
    kinds, scopes = ArtifactKind, ReadScope
    planner = AgentSpec(
        role=PLANNER,
        routing_role=RoutingRole.PLANNER,
        prompt_ref=PLAN_PROMPT,
        read_scope=(ReadGrant(kind=kinds.QUESTION, scope=scopes.ALL),),
    )
    reasoner_reads = (
        ReadGrant(kind=kinds.QUESTION, scope=scopes.ALL),
        ReadGrant(kind=kinds.FRAME, scope=scopes.ALL),
        ReadGrant(kind=kinds.PERSPECTIVE, scope=scopes.OWN),
        ReadGrant(kind=kinds.CRITIQUE, scope=scopes.ADDRESSED_TO_SELF),
    )
    if acting_tools:
        reasoner = AgentSpec(
            role=REASONER,
            kind=AgentKind.ACTING,
            routing_role=RoutingRole.SYNTHESIZER,
            prompt_ref=EXECUTE_PROMPT,
            read_scope=(*reasoner_reads, ReadGrant(kind=kinds.EVIDENCE, scope=scopes.ALL)),
            tool_policy=ToolPolicy(allowed_tools=tuple(acting_tools)),
        )
    else:
        reasoner = AgentSpec(
            role=REASONER,
            routing_role=RoutingRole.SYNTHESIZER,
            prompt_ref=EXECUTE_PROMPT,
            read_scope=reasoner_reads,
        )
    reviewer = AgentSpec(
        role=REVIEWER,
        routing_role=RoutingRole.REVIEWER,
        prompt_ref=REVIEW_PROMPT,
        read_scope=(
            ReadGrant(kind=kinds.QUESTION, scope=scopes.ALL),
            ReadGrant(kind=kinds.FRAME, scope=scopes.ALL),
            ReadGrant(kind=kinds.PERSPECTIVE, scope=scopes.ASSIGNED),
        ),
    )
    return planner, reasoner, reviewer


DEFAULT_ASSIGNMENTS: Mapping[AgentRole, tuple[AgentRole, ...]] = MappingProxyType(
    {REVIEWER: (REASONER,)}
)


class PlanReviewDriver:
    """Drives one plan, execute, observe, reflect, validate run. Holds no per-run state."""

    def __init__(
        self,
        registry: AgentRegistry,
        runner: AgentRunner,
        ids: IdSource,
        *,
        max_attempts: int = 3,
        validator: Validator = non_blank,
        assignments: AssignmentPolicy | None = None,
        acting_runner: ActingPort | None = None,
        execution_budget: ExecutionBudget | None = None,
    ) -> None:
        if not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
            raise DriverConfigError(f"max_attempts must be between 1 and {MAX_ATTEMPTS_LIMIT}")
        for role in (PLANNER, REASONER, REVIEWER):
            if role not in registry:
                raise DriverConfigError(f"the {role.value} agent is not registered")
        for role in (PLANNER, REVIEWER):
            if registry.get(role).kind is not AgentKind.REASONING:
                raise DriverConfigError(
                    f"the {role.value} agent must be reasoning-only; acting agents cannot run yet"
                )
        reasoner_acts = registry.get(REASONER).kind is AgentKind.ACTING
        if reasoner_acts and acting_runner is None:
            raise DriverConfigError(
                "the reasoner agent must be reasoning-only unless an acting runner is supplied"
            )
        if acting_runner is not None and not reasoner_acts:
            raise DriverConfigError("an acting runner was supplied but the reasoner is not acting")
        self._registry = registry
        self._acting = acting_runner
        self._execution_budget = execution_budget
        self._runner = runner
        self._ids = ids
        self._max_attempts = max_attempts
        self._validator = validator
        self._builder = ContextBuilder(
            assignments if assignments is not None else StaticAssignments(DEFAULT_ASSIGNMENTS)
        )

    async def run(
        self, task: str, *, run_id: str | None = None, handle: RunHandle | None = None
    ) -> DriverResult:
        """Run to a terminal state. Raises only ``asyncio.CancelledError``, after cancelling the run."""
        handle = handle if handle is not None else RunHandle()
        handle.state = RunState.start(
            run_id or self._ids.next_id("run"), max_attempts=self._max_attempts
        )
        store = ArtifactStore(self._ids)
        ctx = _RunContext()
        handle._context = ctx
        try:
            if not task.strip():
                self._fail(handle, "invalid_task", "the task is empty")
                return self._result(handle)
            store.put(
                kind=ArtifactKind.QUESTION,
                text=task.strip(),
                author=None,
                phase=RunPhase.PLAN,
                attempt=1,
            )
            return await self._drive(handle, store, ctx)
        except asyncio.CancelledError:
            # Signal the run's control synchronously and never await ``drain()`` here: a tool the
            # executor stopped waiting on keeps running and stays tracked in the control. Nothing
            # claims it was stopped.
            if ctx.control is not None:
                ctx.control.token.cancel()
            state = self._state(handle)
            if not state.is_terminal:
                handle.state = cancel(state)
            self._snapshot(handle)
            raise
        except (
            Exception
        ) as exc:  # a driver or machine defect: end the run, do not leak it half-done
            state = self._state(handle)
            if state.is_terminal:
                raise
            self._fail(handle, "internal", f"driver error: {type(exc).__name__}")
            return self._result(handle)

    # ── the phases ────────────────────────────────────────────────────────────

    async def _drive(
        self, handle: RunHandle, store: ArtifactStore, ctx: _RunContext
    ) -> DriverResult:
        # PLAN
        plan = await self._agent_phase(handle, store, PLANNER, Step.PLAN, ArtifactKind.FRAME)
        if plan is None:
            return self._result(handle)
        handle.state = advance(self._state(handle), RunPhase.EXECUTE)

        while True:
            # EXECUTE
            draft = await self._execute_phase(handle, store, ctx)
            if draft is None:
                return self._result(handle)
            if not self._settled(handle, ctx):
                return self._result(handle)
            handle.state = advance(self._state(handle), RunPhase.OBSERVE)

            # OBSERVE: the driver only confirms this attempt produced the draft it will review.
            if store.latest(ArtifactKind.PERSPECTIVE, author=REASONER) is not draft:
                self._fail(handle, "internal", "no draft to observe")
                return self._result(handle)
            handle.state = advance(self._state(handle), RunPhase.REFLECT)

            # REFLECT
            review = await self._agent_phase(
                handle,
                store,
                REVIEWER,
                Step.REVIEW,
                ArtifactKind.CRITIQUE,
                target=REASONER,
                accept=_needs_verdict,
            )
            if review is None:
                return self._result(handle)
            if parse_verdict(review.text) is Verdict.APPROVE:
                break
            if not self._settled(handle, ctx):
                return self._result(handle)
            try:
                handle.state = advance(self._state(handle), RunPhase.RETRY)
            except RetryLimitExceeded:
                self._fail(
                    handle, "retry_limit", f"not approved after {self._max_attempts} attempts"
                )
                return self._result(handle)
            handle.state = advance(self._state(handle), RunPhase.EXECUTE)

        # VALIDATE
        if not self._settled(handle, ctx):
            return self._result(handle)
        handle.state = advance(self._state(handle), RunPhase.VALIDATE)
        try:
            reason = self._validator(draft.text)
        except Exception as exc:
            reason = f"validator raised {type(exc).__name__}"
        if reason:
            self._fail(handle, "validation_failed", reason)
            return self._result(handle)
        if not self._settled(handle, ctx):
            return self._result(handle)
        handle.state = advance(self._state(handle), RunPhase.FINISH)
        return self._result(handle, answer=draft.text)

    async def _agent_phase(
        self,
        handle: RunHandle,
        store: ArtifactStore,
        role: AgentRole,
        step: Step,
        kind: ArtifactKind,
        *,
        target: AgentRole | None = None,
        accept: Callable[[str], str | None] | None = None,
    ) -> StoredArtifact | None:
        """Run one agent over trusted context. Returns the stored artifact, or ``None`` after failing the run."""
        state = self._state(handle)
        spec = self._registry.get(role)
        outcome = await self._runner.run(
            spec,
            AgentInput(
                run_id=state.run_id,
                attempt=state.attempt,
                instruction=INSTRUCTIONS[step],
                context=self._context(spec, store),
            ),
        )
        if not outcome.ok:
            failure = outcome.failure
            kind_name = f"agent_{failure.kind.value}"[:40]  # type: ignore[union-attr]
            self._fail(handle, kind_name, f"{role.value}: {failure.message}")  # type: ignore[union-attr]
            return None
        text = outcome.text or ""
        if accept is not None:
            reason = accept(text)
            if reason:
                self._fail(handle, "invalid_output", f"{role.value}: {reason}")
                return None
        artifact = store.put(
            kind=kind,
            text=text,
            author=role,
            phase=state.phase,
            attempt=state.attempt,
            target=target,
        )
        handle.state = record_artifact(
            state,
            RunArtifact(
                artifact_id=artifact.artifact_id,
                kind=kind,
                author=role,
                phase=state.phase,
                attempt=state.attempt,
                digest=artifact.digest,
            ),
        )
        return artifact

    # ── acting (Phase 4D-4c) ──────────────────────────────────────────────────

    async def _execute_phase(
        self, handle: RunHandle, store: ArtifactStore, ctx: _RunContext
    ) -> StoredArtifact | None:
        """EXECUTE. Reasoning-only unless an acting runner was supplied at construction."""
        if self._acting is None:
            return await self._agent_phase(
                handle, store, REASONER, Step.EXECUTE, ArtifactKind.PERSPECTIVE
            )
        state = self._state(handle)
        spec = self._registry.get(REASONER)
        if ctx.control is None:
            # Exactly one control per run, created at the first EXECUTE (so the wall-clock budget
            # measures tool time, not planning) and reused by every attempt and turn. Nothing here
            # ever recreates or resets it, and RETRY does not touch it.
            ctx.control = RunControl.create(state.run_id, self._execution_budget)
        self._reconcile(ctx)
        try:
            outcome = await self._acting.run_acting(
                spec,
                AgentInput(
                    run_id=state.run_id,
                    attempt=state.attempt,
                    instruction=INSTRUCTIONS[Step.EXECUTE],
                    context=self._context(spec, store),
                ),
                control=ctx.control,
                blocked_digests=frozenset(ctx.unsettled),
            )
        except asyncio.CancelledError as exc:
            self._absorb(ctx, getattr(exc, "tool_records", ()))
            raise
        # Store what the tools returned (success or failure) before deciding anything else.
        self._store_records(handle, store, outcome.tool_records)
        self._absorb(ctx, outcome.tool_records)
        if not outcome.ok:
            failure = outcome.failure
            self._fail(handle, failure.kind.value[:40], f"{spec.role.value}: {failure.message}")
            return None
        if not self._settled(handle, ctx):
            return None
        artifact = store.put(
            kind=ArtifactKind.PERSPECTIVE,
            text=outcome.text,
            author=REASONER,
            phase=state.phase,
            attempt=state.attempt,
        )
        handle.state = record_artifact(
            state,
            RunArtifact(
                artifact_id=artifact.artifact_id,
                kind=ArtifactKind.PERSPECTIVE,
                author=REASONER,
                phase=state.phase,
                attempt=state.attempt,
                digest=artifact.digest,
            ),
        )
        return artifact

    def _store_records(self, handle: RunHandle, store: ArtifactStore, records: Any) -> None:
        """Append one tool-origin EVIDENCE artifact per record. Only the driver ever does this.

        The fields come from the executor's own result as carried by the record, never from model
        text. A tool artifact has no agent author, so it is not added to ``RunState.artifacts``
        (which only points at agent-authored work).
        """
        state = self._state(handle)
        for record in records:
            store.put(
                kind=ArtifactKind.EVIDENCE,
                text=_evidence_text(record),
                author=None,
                phase=state.phase,
                attempt=state.attempt,
                origin=ArtifactOrigin.TOOL,
                source=record.tool,
                tool_meta=ToolArtifactMeta(
                    request_id=record.request_id,
                    call_digest=record.call_digest,
                    status=record.status.value,
                    outcome=record.outcome.value,
                    executed=record.executed,
                    unsettled=record.unsettled,
                    truncated=record.truncated,
                ),
            )

    @staticmethod
    def _absorb(ctx: _RunContext, records: Any) -> None:
        for record in records:
            if getattr(record, "unsettled", False):
                ctx.unsettled[record.call_digest] = record.request_id

    @staticmethod
    def _reconcile(ctx: _RunContext) -> None:
        """Forget an unsettled call only when the run's own idempotency record confirms it settled.

        A cancelled or timed-out wait is not evidence: the executor records such a call as UNKNOWN and
        never moves it, so it stays tracked for the rest of the run.
        """
        control = ctx.control
        if control is None:
            return
        for digest, request_id in list(ctx.unsettled.items()):
            if control.store.state_of(request_id) in _SETTLED_STATES:
                del ctx.unsettled[digest]

    def _settled(self, handle: RunHandle, ctx: _RunContext) -> bool:
        """False, with the run failed, while any tool call is unsettled or abandoned."""
        self._reconcile(ctx)
        control = ctx.control
        if ctx.unsettled or (control is not None and control.abandoned_count > 0):
            self._fail(
                handle, UNSETTLED_KIND, "a tool call has not settled; the run cannot continue"
            )
            return False
        return True

    def _context(self, spec: AgentSpec, store: ArtifactStore) -> tuple[ContextItem, ...]:
        """The context for one agent. Tool-origin items go only to acting agents (defence in depth)."""
        items = self._builder.build(spec, store)
        if spec.kind is AgentKind.ACTING:
            return items
        return tuple(item for item in items if item.origin is not ArtifactOrigin.TOOL)

    def _snapshot(self, handle: RunHandle) -> None:
        ctx = handle._context
        if ctx is None:
            return
        handle.abandoned_count = ctx.control.abandoned_count if ctx.control is not None else 0
        handle.unsettled_calls = len(ctx.unsettled)

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _state(handle: RunHandle) -> RunState:
        assert handle.state is not None
        return handle.state

    def _fail(self, handle: RunHandle, kind: str, message: str) -> None:
        handle.state = fail(self._state(handle), kind=kind, message=message)

    def _result(self, handle: RunHandle, *, answer: str | None = None) -> DriverResult:
        self._snapshot(handle)
        return DriverResult(
            state=self._state(handle),
            answer=answer,
            abandoned_count=handle.abandoned_count,
            unsettled_calls=handle.unsettled_calls,
        )


def _evidence_text(record: Any) -> str:
    """The model-visible text of a tool artifact: the (already redacted) output, or a fixed note.

    The note is built from enum values only, never from an exception or from tool output. The text is
    bounded; the executor has already capped and redacted the output itself.
    """
    output = (record.output or "").strip()
    if output:
        text = output
    elif record.status.value == "ok":
        text = EMPTY_EVIDENCE_TEXT
    else:
        text = f"[the tool call did not complete: {record.outcome.value}]"
    if len(text) > MAX_EVIDENCE_CHARS:
        keep = MAX_EVIDENCE_CHARS - len(EVIDENCE_CLIPPED_MARKER)
        text = text[:keep] + EVIDENCE_CLIPPED_MARKER
    return text


def _needs_verdict(text: str) -> str | None:
    return None if parse_verdict(text) is not None else "the review gave no VERDICT line"
