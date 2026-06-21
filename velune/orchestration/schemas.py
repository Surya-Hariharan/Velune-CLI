"""Typed state schemas for orchestration graph execution."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from velune.core.types.task import TaskPlan


class ExecutionStatus(StrEnum):
    """Execution status for orchestration runs."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    VALIDATING = "validating"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class AgentMessage(BaseModel):
    """Typed message exchanged between cooperating agent nodes."""

    sender: str
    receiver: str
    content: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionAttempt(BaseModel):
    """An attempt record for autonomous retry loops."""

    attempt: int = 1
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime | None = None
    success: bool = False
    issues: list[str] = Field(default_factory=list)


class OrchestrationRequest(BaseModel):
    """Input contract for orchestration execution."""

    prompt: str
    workspace: str
    task_id: str | None = None
    model: str | None = None
    max_retries: int = Field(default=2, ge=0, le=5)
    intent: str | None = None  # IntentType value; typed as str for schema portability
    metadata: dict[str, Any] = Field(default_factory=dict)


class OrchestrationState(BaseModel):
    """Durable state across planning, tooling, reasoning, and validation."""

    run_id: str
    request: OrchestrationRequest
    status: ExecutionStatus = ExecutionStatus.PENDING
    task_plan: TaskPlan | None = None
    attempts: list[ExecutionAttempt] = Field(default_factory=list)
    checkpoints: list[str] = Field(default_factory=list)
    output: str | None = None
    coder_proposal: str | None = None
    error: str | None = None
    validation_issues: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class OrchestrationResult(BaseModel):
    """Final result emitted from orchestration execution."""

    run_id: str
    task_id: str
    success: bool
    status: ExecutionStatus
    output: str | None = None
    error: str | None = None
    plan_steps: int = 0
    attempts: int = 0
    validation_issues: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class StreamProgress(BaseModel):
    """Structured progress data yielded during orchestration streaming."""

    run_id: str
    phase: str
    message: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    elapsed_ms: float | None = None

    def __str__(self) -> str:
        if self.phase:
            return f"[{self.run_id}] {self.phase}: {self.message}"
        return f"[{self.run_id}] {self.message}"
