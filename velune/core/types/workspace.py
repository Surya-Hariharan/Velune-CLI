"""Core workspace type definitions."""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WorkspaceState(str, Enum):
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


class CognitionModel(BaseModel):
    """Live cognitive model of the workspace."""
    workspace_path: str
    state: WorkspaceState
    current_task_id: str | None = None
    file_count: int = 0
    symbol_count: int = 0
    last_indexed: datetime | None = None
    health_score: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
