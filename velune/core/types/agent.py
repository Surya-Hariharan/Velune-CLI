"""Core agent type definitions."""

from velune._compat import StrEnum


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
