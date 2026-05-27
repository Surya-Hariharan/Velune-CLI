"""Event-driven cognition system."""

# Migration compatibility exports
from velune.kernel.bus import CognitiveBus as EventBus
from velune.kernel.schemas import Event
from velune.events.handlers.cognition import CognitionEventHandler
from velune.events.handlers.index import IndexEventHandler
from velune.events.handlers.memory import MemoryEventHandler
from velune.events.handlers.telemetry import TelemetryEventHandler
from velune.events.store.log import EventLog
from velune.events.types.agent import AgentCompleted, AgentFailed, AgentStarted
from velune.events.types.filesystem import FileCreated, FileDeleted, FileModified
from velune.events.types.git import BranchChanged, CommitCreated, StashPushed
from velune.events.types.task import PlanUpdated, TaskCompleted, TaskCreated
from velune.events.types.terminal import CommandExecuted, ErrorOccurred, TestRan

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
    "MemoryEventHandler",
    "CognitionEventHandler",
    "IndexEventHandler",
    "TelemetryEventHandler",
    "EventLog",
]
