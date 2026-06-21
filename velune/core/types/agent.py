"""Core agent type definitions."""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AgentRole(StrEnum):
    """Agent role definitions."""

    PLANNER = "planner"
    CODER = "coder"
    REASONER = "reasoner"
    REVIEWER = "reviewer"
    DEBUGGER = "debugger"
    SUMMARIZER = "summarizer"
    RETRIEVER = "retriever"
    SUPERVISOR = "supervisor"


class AgentMessageType(StrEnum):
    """Typed message protocol for agent communication."""

    TASK_REQUEST = "task_request"
    TASK_RESPONSE = "task_response"
    STATUS_UPDATE = "status_update"
    ERROR_REPORT = "error_report"
    QUERY = "query"
    RESPONSE = "response"
    CONTROL = "control"


class AgentMessage(BaseModel):
    """Typed message for inter-agent communication."""

    message_type: AgentMessageType
    sender: str
    recipient: str
    content: Any
    timestamp: float
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResult(BaseModel):
    """Result from agent execution."""

    success: bool
    output: Any | None = None
    error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    execution_time_ms: float | None = None
