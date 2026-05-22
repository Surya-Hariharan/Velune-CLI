"""Core workspace type definitions."""

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field
from datetime import datetime


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
    current_task_id: Optional[str] = None
    file_count: int = 0
    symbol_count: int = 0
    last_indexed: Optional[datetime] = None
    health_score: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
