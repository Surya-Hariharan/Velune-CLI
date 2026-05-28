"""Event-driven cognition system."""

# Migration compatibility exports
from velune.kernel.bus import CognitiveBus as EventBus
from velune.kernel.schemas import Event
from velune.events.types import (
    AgentCompleted,
    AgentFailed,
    AgentStarted,
    BranchChanged,
    CommitCreated,
    CommandExecuted,
    ErrorOccurred,
    FileCreated,
    FileDeleted,
    FileModified,
    PlanUpdated,
    StashPushed,
    TaskCompleted,
    TaskCreated,
    TestRan,
)

__all__ = [
    "EventBus",
    "Event",
    "FileCreated",
    "FileModified",
    "FileDeleted",
    "CommitCreated",
    "BranchChanged",
    "StashPushed",
    "CommandExecuted",
    "ErrorOccurred",
    "TestRan",
    "AgentStarted",
    "AgentCompleted",
    "AgentFailed",
    "TaskCreated",
    "TaskCompleted",
    "PlanUpdated",
]
