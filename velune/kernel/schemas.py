"""Strictly-typed schemas for the Cognitive Kernel."""

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ComponentStatus(str, Enum):
    """Execution status of registered kernel components."""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    SHUTTING_DOWN = "shutting_down"
    SHUTDOWN = "shutdown"


class Event(BaseModel):
    """The central message token in the event bus."""
    event_id: str = Field(default_factory=lambda: f"evt-{uuid.uuid4().hex[:12]}")
    event_type: str
    timestamp: float = Field(default_factory=lambda: datetime.now(tz=UTC).timestamp())
    source: str
    data: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None

    class Config:
        frozen = True


class HealthReport(BaseModel):
    """A report for individual subsystem health."""
    status: ComponentStatus
    latency_ms: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)
    last_check: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
