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

Nothing here calls a provider, executes a tool, reads the CLI, or persists anything.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from enum import Enum
from types import MappingProxyType

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
)
from velune.orchestration.engine.errors import DriverConfigError, RetryLimitExceeded
from velune.orchestration.engine.machine import advance, cancel, fail, record_artifact
from velune.orchestration.engine.registry import AgentRegistry
from velune.orchestration.engine.runner import AgentInput, AgentKind, AgentRunner
from velune.orchestration.engine.spec import AgentSpec, ReadGrant
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


class RunHandle:
    """Where a caller can read the run's latest state, including after a cancellation."""

    def __init__(self) -> None:
        self.state: RunState | None = None


class DriverResult(Contract):
    state: RunState
    answer: str | None = Field(default=None)


def plan_review_specs() -> tuple[AgentSpec, AgentSpec, AgentSpec]:
    """The planner, reasoner and reviewer specs, each reasoning-only with the narrowest grants."""
    kinds, scopes = ArtifactKind, ReadScope
    planner = AgentSpec(
        role=PLANNER,
        routing_role=RoutingRole.PLANNER,
        prompt_ref=PLAN_PROMPT,
        read_scope=(ReadGrant(kind=kinds.QUESTION, scope=scopes.ALL),),
    )
    reasoner = AgentSpec(
        role=REASONER,
        routing_role=RoutingRole.SYNTHESIZER,
        prompt_ref=EXECUTE_PROMPT,
        read_scope=(
            ReadGrant(kind=kinds.QUESTION, scope=scopes.ALL),
            ReadGrant(kind=kinds.FRAME, scope=scopes.ALL),
            ReadGrant(kind=kinds.PERSPECTIVE, scope=scopes.OWN),
            ReadGrant(kind=kinds.CRITIQUE, scope=scopes.ADDRESSED_TO_SELF),
        ),
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
    ) -> None:
        if not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
            raise DriverConfigError(f"max_attempts must be between 1 and {MAX_ATTEMPTS_LIMIT}")
        for role in (PLANNER, REASONER, REVIEWER):
            if role not in registry:
                raise DriverConfigError(f"the {role.value} agent is not registered")
            if registry.get(role).kind is not AgentKind.REASONING:
                raise DriverConfigError(
                    f"the {role.value} agent must be reasoning-only; acting agents cannot run yet"
                )
        self._registry = registry
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
            return await self._drive(handle, store)
        except asyncio.CancelledError:
            state = self._state(handle)
            if not state.is_terminal:
                handle.state = cancel(state)
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

    async def _drive(self, handle: RunHandle, store: ArtifactStore) -> DriverResult:
        # PLAN
        plan = await self._agent_phase(handle, store, PLANNER, Step.PLAN, ArtifactKind.FRAME)
        if plan is None:
            return self._result(handle)
        handle.state = advance(self._state(handle), RunPhase.EXECUTE)

        while True:
            # EXECUTE
            draft = await self._agent_phase(
                handle, store, REASONER, Step.EXECUTE, ArtifactKind.PERSPECTIVE
            )
            if draft is None:
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
            try:
                handle.state = advance(self._state(handle), RunPhase.RETRY)
            except RetryLimitExceeded:
                self._fail(
                    handle, "retry_limit", f"not approved after {self._max_attempts} attempts"
                )
                return self._result(handle)
            handle.state = advance(self._state(handle), RunPhase.EXECUTE)

        # VALIDATE
        handle.state = advance(self._state(handle), RunPhase.VALIDATE)
        try:
            reason = self._validator(draft.text)
        except Exception as exc:
            reason = f"validator raised {type(exc).__name__}"
        if reason:
            self._fail(handle, "validation_failed", reason)
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
                context=self._builder.build(spec, store),
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

    # ── helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _state(handle: RunHandle) -> RunState:
        assert handle.state is not None
        return handle.state

    def _fail(self, handle: RunHandle, kind: str, message: str) -> None:
        handle.state = fail(self._state(handle), kind=kind, message=message)

    def _result(self, handle: RunHandle, *, answer: str | None = None) -> DriverResult:
        return DriverResult(state=self._state(handle), answer=answer)


def _needs_verdict(text: str) -> str | None:
    return None if parse_verdict(text) is not None else "the review gave no VERDICT line"
