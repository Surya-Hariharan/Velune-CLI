"""Agent event types."""

from dataclasses import dataclass
from typing import Dict, Any
from velune.events.bus.engine import Event


@dataclass
class AgentStarted(Event):
    """Event emitted when an agent starts."""
    agent_id: str
    agent_role: str
    task_id: str


@dataclass
class AgentCompleted(Event):
    """Event emitted when an agent completes."""
    agent_id: str
    agent_role: str
    task_id: str
    duration_ms: float


@dataclass
class AgentFailed(Event):
    """Event emitted when an agent fails."""
    agent_id: str
    agent_role: str
    task_id: str
    error_message: str
