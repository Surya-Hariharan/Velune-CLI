"""The unified action model: every state change the agent wants becomes an ``Action``."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ActionType(StrEnum):
    READ = "READ"
    CREATE_FILE = "CREATE_FILE"
    MODIFY_FILE = "MODIFY_FILE"
    DELETE_FILE = "DELETE_FILE"
    CREATE_DIRECTORY = "CREATE_DIRECTORY"
    DELETE_DIRECTORY = "DELETE_DIRECTORY"
    MOVE = "MOVE"
    RUN_COMMAND = "RUN_COMMAND"
    GIT_WRITE = "GIT_WRITE"
    NETWORK = "NETWORK"
    WRITE_PLAN = "WRITE_PLAN"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Action types that change state. READ never does; WRITE_PLAN only touches
# Velune's own plan files and is governed separately by the policy.
MUTATING_TYPES = frozenset(
    {
        ActionType.CREATE_FILE,
        ActionType.MODIFY_FILE,
        ActionType.DELETE_FILE,
        ActionType.CREATE_DIRECTORY,
        ActionType.DELETE_DIRECTORY,
        ActionType.MOVE,
        ActionType.RUN_COMMAND,
        ActionType.GIT_WRITE,
        ActionType.NETWORK,
    }
)


@dataclass(frozen=True, slots=True)
class Action:
    """One proposed operation, described before anything is executed.

    ``target`` is a path (absolute, resolved) or a command line. ``detail``
    carries extra context for the approval preview (e.g. a move destination).
    ``blocked`` marks an operation no mode may run (e.g. ``sudo``).
    """

    action_type: ActionType
    target: str
    reason: str = ""
    risk: Risk = Risk.LOW
    outside_workspace: bool = False
    secret: bool = False
    blocked: bool = False
    detail: str = ""

    @property
    def mutating(self) -> bool:
        return self.action_type in MUTATING_TYPES

    def describe(self) -> str:
        """One-line, human-readable form for previews and the audit log."""
        label = self.action_type.value.replace("_", " ")
        text = f"{label} {self.target}"
        if self.detail:
            text += f" → {self.detail}"
        return text
