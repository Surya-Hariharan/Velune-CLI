"""The typed, immutable record of one engine run.

A :class:`RunState` is a value, not a place to put things. It holds the phase, the attempt counter,
references to the artifacts the run produced (a kind, an author and a digest, never the content) and
the terminal status. It is frozen, strict (unknown fields are rejected) and JSON-serializable, so it
can be written down and read back unchanged. The legal ways to *change* one live in
``velune.orchestration.engine.machine``; nothing here reaches a provider or a disk.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints, model_validator

from velune._compat import StrEnum
from velune.core.types.agent import AgentRole
from velune.council.domain import ArtifactKind
from velune.council.serialization import SCHEMA_VERSION, Contract

RunId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]
ArtifactId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]
Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12,64}$")]
ShortText = Annotated[str, StringConstraints(max_length=300)]

MAX_ATTEMPTS_LIMIT = 10
MAX_ARTIFACTS = 256


class RunPhase(StrEnum):
    """Where a run is. PLAN, EXECUTE, OBSERVE, REFLECT, RETRY and VALIDATE are working phases."""

    PLAN = "plan"
    EXECUTE = "execute"
    OBSERVE = "observe"
    REFLECT = "reflect"
    RETRY = "retry"
    VALIDATE = "validate"
    FINISH = "finish"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_PHASES


TERMINAL_PHASES: frozenset[RunPhase] = frozenset(
    {RunPhase.FINISH, RunPhase.FAILED, RunPhase.CANCELLED}
)


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


_STATUS_OF_TERMINAL: dict[RunPhase, RunStatus] = {
    RunPhase.FINISH: RunStatus.SUCCEEDED,
    RunPhase.FAILED: RunStatus.FAILED,
    RunPhase.CANCELLED: RunStatus.CANCELLED,
}


def status_for(phase: RunPhase) -> RunStatus:
    """The status a run in ``phase`` must have: RUNNING until it reaches a terminal phase."""
    return _STATUS_OF_TERMINAL.get(phase, RunStatus.RUNNING)


class RunFailure(Contract):
    """Why a run failed. A stable kind plus a short message; never prompt or completion text."""

    kind: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
    message: ShortText = ""


class RunArtifact(Contract):
    """A pointer to something the run produced. The content lives elsewhere, addressed by digest."""

    artifact_id: ArtifactId
    kind: ArtifactKind
    author: AgentRole
    phase: RunPhase
    attempt: int = Field(ge=1, le=MAX_ATTEMPTS_LIMIT)
    digest: Digest


class RunState(Contract):
    schema_version: int = SCHEMA_VERSION
    run_id: RunId
    phase: RunPhase = RunPhase.PLAN
    attempt: int = Field(default=1, ge=1, le=MAX_ATTEMPTS_LIMIT)
    max_attempts: int = Field(default=3, ge=1, le=MAX_ATTEMPTS_LIMIT)
    status: RunStatus = RunStatus.RUNNING
    artifacts: tuple[RunArtifact, ...] = Field(default=(), max_length=MAX_ARTIFACTS)
    failure: RunFailure | None = None
    # Every phase the run has been in, oldest first; the last entry is always ``phase``.
    trail: tuple[RunPhase, ...] = (RunPhase.PLAN,)

    @model_validator(mode="after")
    def _consistent(self) -> RunState:
        if self.attempt > self.max_attempts:
            raise ValueError("attempt cannot exceed max_attempts")
        if self.status is not status_for(self.phase):
            raise ValueError(
                f"a run in {self.phase.value} must have status {status_for(self.phase).value}"
            )
        if (self.phase is RunPhase.FAILED) != (self.failure is not None):
            raise ValueError("a failed run carries a failure, and no other run does")
        if not self.trail or self.trail[-1] is not self.phase:
            raise ValueError("trail must end at the current phase")
        if any(item.is_terminal for item in self.trail[:-1]):
            raise ValueError("trail cannot continue past a terminal phase")
        if any(artifact.attempt > self.attempt for artifact in self.artifacts):
            raise ValueError("an artifact cannot belong to a future attempt")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.phase.is_terminal

    @classmethod
    def start(cls, run_id: str, *, max_attempts: int = 3) -> RunState:
        return cls(run_id=run_id, max_attempts=max_attempts)
