"""Typed errors of the orchestration engine.

Every error names what was wrong and what the caller can do about it, so a failure is actionable
from its message alone. None of them carries prompt text, completions or credentials.
"""

from __future__ import annotations


class EngineError(Exception):
    """Base class for every error the orchestration engine raises on purpose."""


class DuplicateAgentError(EngineError):
    """An agent role was registered twice. Registration never replaces an existing spec."""

    def __init__(self, role: str) -> None:
        self.role = role
        super().__init__(
            f"agent {role!r} is already registered; registry entries are never replaced "
            "(build a new AgentRegistry to change the set)"
        )


class UnknownAgentError(EngineError, LookupError):
    """A lookup named an agent that is not registered. There are no implicit default agents."""

    def __init__(self, role: str, registered: tuple[str, ...]) -> None:
        self.role = role
        self.registered = registered
        known = ", ".join(registered) if registered else "none"
        super().__init__(f"unknown agent {role!r}; registered agents: {known}")


class IllegalTransitionError(EngineError):
    """A run phase change that the transition table does not allow."""

    def __init__(self, source: str, target: str, allowed: tuple[str, ...]) -> None:
        self.source = source
        self.target = target
        self.allowed = allowed
        options = ", ".join(allowed) if allowed else "none"
        super().__init__(
            f"cannot move a run from {source} to {target}; allowed from {source}: {options}"
        )


class TerminalStateError(IllegalTransitionError):
    """A transition was attempted out of FINISH, FAILED or CANCELLED. Terminal states are final."""

    def __init__(self, source: str, target: str) -> None:
        super().__init__(source, target, ())
        self.args = (
            f"the run already ended in {source}; terminal states cannot transition (to {target})",
        )


class RetryLimitExceeded(EngineError):
    """The run asked for another attempt after using all of them."""

    def __init__(self, attempt: int, max_attempts: int) -> None:
        self.attempt = attempt
        self.max_attempts = max_attempts
        super().__init__(
            f"attempt {attempt} of {max_attempts} was the last; fail the run or validate instead of retrying"
        )


class InvalidArtifactError(EngineError, ValueError):
    """An artifact reference that does not belong in the run it was recorded against."""
