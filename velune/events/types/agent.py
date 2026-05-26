"""Agent event types."""

from dataclasses import dataclass
from typing import Any


@dataclass
class AgentStarted:
    """Event emitted when an agent starts."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    agent_id: str
    agent_role: str
    task_id: str


@dataclass
class AgentCompleted:
    """Event emitted when an agent completes."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    agent_id: str
    agent_role: str
    task_id: str
    duration_ms: float


@dataclass
class AgentFailed:
    """Event emitted when an agent fails."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    agent_id: str
    agent_role: str
    task_id: str
    error_message: str
