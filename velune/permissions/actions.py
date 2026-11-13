"""The unified action model: every state change the agent wants becomes an ``Action``."""

from __future__ import annotations

import re
from dataclasses import dataclass

from velune._compat import StrEnum


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
    # Operations on an application integration (Docker, a database, an MCP
    # server, …). Each one carries a dotted ``scope`` such as
    # ``docker.container.start``; an integration action without a valid scope
    # is refused (fail closed). They are deliberately separate from
    # RUN_COMMAND / NETWORK so integration policy never piggybacks on them.
    INTEGRATION_READ = "INTEGRATION_READ"
    INTEGRATION_WRITE = "INTEGRATION_WRITE"
    INTEGRATION_EXECUTE = "INTEGRATION_EXECUTE"
    INTEGRATION_DELETE = "INTEGRATION_DELETE"


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


INTEGRATION_TYPES = frozenset(
    {
        ActionType.INTEGRATION_READ,
        ActionType.INTEGRATION_WRITE,
        ActionType.INTEGRATION_EXECUTE,
        ActionType.INTEGRATION_DELETE,
    }
)

# ``<integration>.<segment>[.<segment>…]``: lowercase, at least two segments,
# no wildcards. Scopes are compared for exact equality only — there is no
# prefix or substring matching anywhere in the policy.
_SCOPE_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_-]*)+$")
MAX_SCOPE_LENGTH = 160


def valid_scope(scope: str) -> bool:
    """True when *scope* is a well-formed integration scope."""
    return bool(scope) and len(scope) <= MAX_SCOPE_LENGTH and bool(_SCOPE_RE.match(scope))


def scope_segment(text: str) -> str:
    """Normalize arbitrary text (an MCP server or tool name) into one scope segment."""
    seg = re.sub(r"[^a-z0-9_-]+", "_", text.lower()).strip("_-")
    if not seg or not seg[0].isalnum():
        seg = f"x{seg}"
    return seg[:48]


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
        ActionType.INTEGRATION_WRITE,
        ActionType.INTEGRATION_EXECUTE,
        ActionType.INTEGRATION_DELETE,
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
    # Integration scope (``docker.container.start``). Required for, and only
    # meaningful on, the INTEGRATION_* action types.
    scope: str = ""

    @property
    def mutating(self) -> bool:
        return self.action_type in MUTATING_TYPES

    @property
    def integration(self) -> bool:
        return self.action_type in INTEGRATION_TYPES

    @property
    def high_risk(self) -> bool:
        """HIGH risk, or an integration delete (always confirmed by a human)."""
        return self.risk is Risk.HIGH or self.action_type is ActionType.INTEGRATION_DELETE

    @property
    def changes_state(self) -> bool:
        """Anything other than reading — including writing a plan file."""
        return self.mutating or self.action_type is ActionType.WRITE_PLAN

    def describe(self) -> str:
        """One-line, human-readable form for previews and the audit log."""
        label = self.action_type.value.replace("_", " ")
        text = f"{label} {self.target}"
        if self.scope:
            text += f" [{self.scope}]"
        if self.detail:
            text += f" → {self.detail}"
        return text
