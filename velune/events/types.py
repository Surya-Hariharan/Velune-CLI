"""Consolidated event types for the Velune system."""

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


@dataclass
class FileCreated:
    """Event emitted when a file is created."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    file_path: str
    file_size: int


@dataclass
class FileModified:
    """Event emitted when a file is modified."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    file_path: str
    file_size: int


@dataclass
class FileDeleted:
    """Event emitted when a file is deleted."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    file_path: str


@dataclass
class CommitCreated:
    """Event emitted when a commit is created."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    commit_hash: str
    author: str
    message: str
    branch: str


@dataclass
class BranchChanged:
    """Event emitted when the branch is changed."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    old_branch: str
    new_branch: str


@dataclass
class StashPushed:
    """Event emitted when a stash is pushed."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    stash_ref: str


@dataclass
class TaskCreated:
    """Event emitted when a task is created."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    task_id: str
    description: str
    priority: int


@dataclass
class TaskCompleted:
    """Event emitted when a task is completed."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    task_id: str
    success: bool
    duration_ms: float


@dataclass
class PlanUpdated:
    """Event emitted when a plan is updated."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    task_id: str
    steps_count: int


@dataclass
class CommandExecuted:
    """Event emitted when a command is executed."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    command: str
    exit_code: int
    duration_ms: float


@dataclass
class ErrorOccurred:
    """Event emitted when an error occurs."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    error_type: str
    error_message: str
    stack_trace: str


@dataclass
class TestRan:
    """Event emitted when a test is run."""
    event_type: str
    data: dict[str, Any]
    timestamp: float
    source: str
    test_name: str
    passed: bool
    duration_ms: float
