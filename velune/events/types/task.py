"""Task event types."""

from dataclasses import dataclass

from velune.events.bus.engine import Event


@dataclass
class TaskCreated(Event):
    """Event emitted when a task is created."""
    task_id: str
    description: str
    priority: int


@dataclass
class TaskCompleted(Event):
    """Event emitted when a task is completed."""
    task_id: str
    success: bool
    duration_ms: float


@dataclass
class PlanUpdated(Event):
    """Event emitted when a plan is updated."""
    task_id: str
    steps_count: int
