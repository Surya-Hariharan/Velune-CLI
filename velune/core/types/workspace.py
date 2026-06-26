"""Core workspace type definitions."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from velune._compat import StrEnum


class WorkspaceState(StrEnum):
    """Workspace state machine states."""

    IDLE = "idle"
    TASK_ACTIVE = "task_active"
    DEBUGGING = "debugging"
    REVIEWING = "reviewing"
    INDEXING = "indexing"
    ERROR = "error"


class WorkspaceEvent(BaseModel):
    """An event in the workspace."""

    event_type: str
    timestamp: datetime
    source: str
    data: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
