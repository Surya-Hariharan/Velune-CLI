"""Core task type definitions."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class TaskStatus(StrEnum):
    """Task execution status."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Task(BaseModel):
    """A task to be executed."""
    id: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = Field(default=5, ge=1, le=10)
    dependencies: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskStep(BaseModel):
    """A step within a task plan."""
    id: str
    description: str
    agent_role: str
    status: TaskStatus = TaskStatus.PENDING
    dependencies: list[str] = Field(default_factory=list)
    estimated_duration_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskPlan(BaseModel):
    """A plan for executing a task."""
    task_id: str
    steps: list[TaskStep]
    estimated_duration_ms: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskResult(BaseModel):
    """Result from task execution."""
    task_id: str
    success: bool
    output: Any | None = None
    error: str | None = None
    steps_completed: int
    steps_total: int
    execution_time_ms: float
    metadata: dict[str, Any] = Field(default_factory=dict)
