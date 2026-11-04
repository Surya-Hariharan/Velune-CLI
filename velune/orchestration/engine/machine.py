r"""The run state machine: which phase changes are legal, and nothing else.

Pure functions over :class:`RunState`. Every call returns a new state or raises a typed error; none
of them awaits, calls a provider, reads a clock or touches a file, so the whole protocol is
exercised with plain values.

    PLAN -> EXECUTE -> OBSERVE -> REFLECT -> VALIDATE -> FINISH
                                       \-> RETRY -> EXECUTE   (bounded by ``max_attempts``)

Any working phase may also move to FAILED or CANCELLED. FINISH, FAILED and CANCELLED are terminal:
nothing leaves them, including a second cancel.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from velune.orchestration.engine.errors import (
    IllegalTransitionError,
    InvalidArtifactError,
    RetryLimitExceeded,
    TerminalStateError,
)
from velune.orchestration.state.run_state import (
    MAX_ARTIFACTS,
    TERMINAL_PHASES,
    RunArtifact,
    RunFailure,
    RunPhase,
    RunState,
    status_for,
)

_P = RunPhase
_ABORTS = (_P.FAILED, _P.CANCELLED)

TRANSITIONS: Mapping[RunPhase, frozenset[RunPhase]] = MappingProxyType(
    {
        _P.PLAN: frozenset({_P.EXECUTE, *_ABORTS}),
        _P.EXECUTE: frozenset({_P.OBSERVE, *_ABORTS}),
        _P.OBSERVE: frozenset({_P.REFLECT, *_ABORTS}),
        _P.REFLECT: frozenset({_P.RETRY, _P.VALIDATE, *_ABORTS}),
        _P.RETRY: frozenset({_P.EXECUTE, *_ABORTS}),
        _P.VALIDATE: frozenset({_P.FINISH, *_ABORTS}),
        _P.FINISH: frozenset(),
        _P.FAILED: frozenset(),
        _P.CANCELLED: frozenset(),
    }
)


def allowed_from(phase: RunPhase) -> tuple[RunPhase, ...]:
    """The phases ``phase`` may move to, in a stable order."""
    return tuple(sorted(TRANSITIONS[phase], key=lambda item: item.value))


def can_transition(source: RunPhase, target: RunPhase) -> bool:
    return target in TRANSITIONS[source]


def _evolve(state: RunState, **changes: object) -> RunState:
    """A validated copy. Going through validation keeps every invariant of ``RunState`` live."""
    return RunState.model_validate({**state.model_dump(), **changes})


def _check(state: RunState, target: RunPhase) -> None:
    if state.phase in TERMINAL_PHASES:
        raise TerminalStateError(state.phase.value, target.value)
    if not can_transition(state.phase, target):
        raise IllegalTransitionError(
            state.phase.value,
            target.value,
            tuple(item.value for item in allowed_from(state.phase)),
        )


def advance(state: RunState, target: RunPhase) -> RunState:
    """Move to the next working phase (or FINISH). Use :func:`fail` and :func:`cancel` to abort.

    Entering RETRY is the only step that spends an attempt, so a run can never exceed
    ``max_attempts``: asking for a retry after the last attempt raises ``RetryLimitExceeded``.
    """
    if target in _ABORTS:
        # Terminal check first so a finished run reports that, not a misleading hint.
        if state.phase in TERMINAL_PHASES:
            raise TerminalStateError(state.phase.value, target.value)
        raise ValueError(f"use fail() or cancel() to move a run to {target.value}")
    _check(state, target)
    attempt = state.attempt
    if target is RunPhase.RETRY:
        if state.attempt >= state.max_attempts:
            raise RetryLimitExceeded(state.attempt, state.max_attempts)
        attempt += 1
    return _evolve(
        state,
        phase=target,
        attempt=attempt,
        status=status_for(target),
        trail=(*state.trail, target),
    )


def fail(state: RunState, *, kind: str, message: str = "") -> RunState:
    """End the run as FAILED, recording why. ``kind`` is a stable lowercase identifier."""
    _check(state, RunPhase.FAILED)
    return _evolve(
        state,
        phase=RunPhase.FAILED,
        status=status_for(RunPhase.FAILED),
        failure=RunFailure(kind=kind, message=message[:300]),
        trail=(*state.trail, RunPhase.FAILED),
    )


def cancel(state: RunState) -> RunState:
    """End the run as CANCELLED. Cancelling a run that already ended raises ``TerminalStateError``."""
    _check(state, RunPhase.CANCELLED)
    return _evolve(
        state,
        phase=RunPhase.CANCELLED,
        status=status_for(RunPhase.CANCELLED),
        trail=(*state.trail, RunPhase.CANCELLED),
    )


def record_artifact(state: RunState, artifact: RunArtifact) -> RunState:
    """Attach an artifact reference produced in the current phase of the current attempt."""
    if state.phase in TERMINAL_PHASES:
        raise TerminalStateError(state.phase.value, "record_artifact")
    if artifact.phase is not state.phase or artifact.attempt != state.attempt:
        raise InvalidArtifactError(
            f"artifact {artifact.artifact_id!r} was made in {artifact.phase.value} "
            f"attempt {artifact.attempt}, but the run is in {state.phase.value} "
            f"attempt {state.attempt}"
        )
    if any(item.artifact_id == artifact.artifact_id for item in state.artifacts):
        raise InvalidArtifactError(f"artifact {artifact.artifact_id!r} is already recorded")
    if len(state.artifacts) >= MAX_ARTIFACTS:
        raise InvalidArtifactError(f"a run holds at most {MAX_ARTIFACTS} artifact references")
    return _evolve(state, artifacts=(*state.artifacts, artifact))
