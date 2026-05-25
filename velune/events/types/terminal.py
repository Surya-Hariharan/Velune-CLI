"""Terminal event types."""

from dataclasses import dataclass

from velune.events.bus.engine import Event


@dataclass
class CommandExecuted(Event):
    """Event emitted when a command is executed."""
    command: str
    exit_code: int
    duration_ms: float


@dataclass
class ErrorOccurred(Event):
    """Event emitted when an error occurs."""
    error_type: str
    error_message: str
    stack_trace: str


@dataclass
class TestRan(Event):
    """Event emitted when a test is run."""
    test_name: str
    passed: bool
    duration_ms: float
