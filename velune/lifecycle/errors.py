"""Shared result and error model for the lifecycle commands (update/repair/uninstall).

One exit-code table and one JSON shape for every lifecycle command, so the
commands cannot drift apart. The table is the one in the Phase 2 design (§9);
do not add codes here without changing that design.

Nothing in this module runs a command or touches the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any

from velune.core.privacy import sanitize_diagnostics


class ExitCode(IntEnum):
    """Process exit codes shared by all lifecycle commands (Phase 2 design §9)."""

    OK = 0  # success / already up to date / dry-run plan printed
    ERROR = 1  # unexpected error
    USAGE = 2  # bad flags, invalid version
    REFUSED = 3  # unsupported install kind, no ownership, unsafe to proceed
    NETWORK = 4  # network or index failure (nothing changed)
    VALIDATION_FAILED = 5  # candidate validation failed (live install untouched)
    APPLY_FAILED_ROLLED_BACK = 6  # apply failed, previous version restored and verified
    ROLLBACK_FAILED = 7  # apply failed and rollback failed (manual recovery printed)
    BUSY = 8  # another lifecycle operation holds the lock
    UPDATE_AVAILABLE = 10  # `update --check`: an update is available
    CANCELLED = 130  # cancelled by the user


class Category(str, Enum):
    """Stable machine-readable name for each exit code (the JSON ``status``)."""

    OK = "ok"
    ERROR = "error"
    USAGE = "usage"
    REFUSED = "refused"
    NETWORK = "network"
    VALIDATION_FAILED = "validation_failed"
    APPLY_FAILED_ROLLED_BACK = "apply_failed_rolled_back"
    ROLLBACK_FAILED = "rollback_failed"
    BUSY = "busy"
    UPDATE_AVAILABLE = "update_available"
    CANCELLED = "cancelled"


# Exactly one category per exit code; a test asserts the two enums stay in step.
_CATEGORY_BY_CODE: dict[ExitCode, Category] = {code: Category[code.name] for code in ExitCode}


def category_for(code: ExitCode) -> Category:
    return _CATEGORY_BY_CODE[code]


class LifecycleError(Exception):
    """A lifecycle operation stopped for a known reason.

    ``message`` says what happened (and, where it matters, what was *not*
    changed). ``recovery`` says what to do next. ``manual_command`` is the exact
    command a user can run themselves, when there is one.
    """

    def __init__(
        self,
        code: ExitCode,
        message: str,
        *,
        recovery: str | None = None,
        manual_command: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery = recovery
        self.manual_command = manual_command
        self.details = details or {}

    @property
    def category(self) -> Category:
        return category_for(self.code)


@dataclass(frozen=True)
class LifecycleResult:
    """Outcome of one lifecycle command, renderable as text or JSON."""

    command: str
    exit_code: ExitCode = ExitCode.OK
    message: str = ""
    recovery: str | None = None
    manual_command: str | None = None
    installation: dict[str, Any] = field(default_factory=dict)
    plan: tuple[str, ...] = ()
    result: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        return category_for(self.exit_code).value

    @property
    def ok(self) -> bool:
        return self.exit_code in (ExitCode.OK, ExitCode.UPDATE_AVAILABLE)

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready dict in the design's shape, with paths and secrets scrubbed."""
        payload: dict[str, Any] = {
            "command": self.command,
            "status": self.status,
            "exit_code": int(self.exit_code),
            "message": self.message,
            "installation": dict(self.installation),
            "plan": list(self.plan),
            "result": dict(self.result),
            "warnings": list(self.warnings),
        }
        if self.recovery or self.manual_command:
            payload["recovery"] = {
                "guidance": self.recovery,
                "manual_command": self.manual_command,
            }
        sanitized: dict[str, Any] = sanitize_diagnostics(payload)
        return sanitized

    def render_text(self) -> str:
        """Plain text, already sanitized the same way as the JSON form."""
        data = self.to_dict()
        lines = [data["message"]] if data["message"] else []
        lines += [f"  - {step}" for step in data["plan"]]
        lines += [f"warning: {w}" for w in data["warnings"]]
        recovery = data.get("recovery")
        if recovery:
            if recovery["guidance"]:
                lines.append(recovery["guidance"])
            if recovery["manual_command"]:
                lines.append(f"Run: {recovery['manual_command']}")
        return "\n".join(lines)

    @classmethod
    def from_error(
        cls, command: str, error: LifecycleError, *, installation: dict[str, Any] | None = None
    ) -> LifecycleResult:
        return cls(
            command=command,
            exit_code=error.code,
            message=error.message,
            recovery=error.recovery,
            manual_command=error.manual_command,
            installation=installation or {},
            result=dict(error.details),
        )

    @classmethod
    def from_exception(
        cls, command: str, exc: BaseException, *, installation: dict[str, Any] | None = None
    ) -> LifecycleResult:
        """Map any exception to a result without leaking its text or a traceback.

        Only the exception *type* is reported: the message of an unexpected
        error can carry paths, URLs or credentials that nobody sanitized.
        """
        if isinstance(exc, LifecycleError):
            return cls.from_error(command, exc, installation=installation)
        if isinstance(exc, KeyboardInterrupt):
            return cls(
                command=command,
                exit_code=ExitCode.CANCELLED,
                message="Cancelled.",
                installation=installation or {},
            )
        return cls(
            command=command,
            exit_code=ExitCode.ERROR,
            message=f"Unexpected error ({type(exc).__name__}). Nothing further was attempted.",
            recovery="Run 'velune doctor' to check the installation, then retry.",
            installation=installation or {},
        )
