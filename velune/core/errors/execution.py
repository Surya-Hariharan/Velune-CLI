"""Execution-related errors."""


class ExecutionError(Exception):
    """Base exception for execution errors."""

    pass


class SandboxError(ExecutionError):
    """Raised when sandbox operation fails."""

    pass


class SnapshotError(ExecutionError):
    """Raised when snapshot operation fails."""

    pass


class RollbackError(ExecutionError):
    """Raised when rollback operation fails."""

    pass


class ValidationError(ExecutionError):
    """Raised when validation fails."""

    pass
