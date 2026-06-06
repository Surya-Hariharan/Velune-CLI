"""Strictly-typed schemas for the Cognitive Kernel."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ComponentStatus(StrEnum):
    """Execution status of registered kernel components."""
    UNINITIALIZED = "uninitialized"
    INITIALIZING = "initializing"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"
    SHUTTING_DOWN = "shutting_down"
    SHUTDOWN = "shutdown"




class HealthReport(BaseModel):
    """A report for individual subsystem health."""
    status: ComponentStatus
    latency_ms: float = 0.0
    details: dict[str, Any] = Field(default_factory=dict)
    last_check: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
