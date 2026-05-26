"""Terminal event types."""

from dataclasses import dataclass
from typing import Any


@dataclass
class CommandExecuted:
    """Event emitted when a command is executed."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    command: str
    exit_code: int
    duration_ms: float


@dataclass
class ErrorOccurred:
    """Event emitted when an error occurs."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    error_type: str
    error_message: str
    stack_trace: str


@dataclass
class TestRan:
    """Event emitted when a test is run."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    test_name: str
    passed: bool
    duration_ms: float
