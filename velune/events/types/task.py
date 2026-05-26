"""Task event types."""

from dataclasses import dataclass
from typing import Any


@dataclass
class TaskCreated:
    """Event emitted when a task is created."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    task_id: str
    description: str
    priority: int


@dataclass
class TaskCompleted:
    """Event emitted when a task is completed."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    task_id: str
    success: bool
    duration_ms: float


@dataclass
class PlanUpdated:
    """Event emitted when a plan is updated."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    task_id: str
    steps_count: int
