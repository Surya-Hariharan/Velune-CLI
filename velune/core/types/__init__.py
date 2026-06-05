"""Core type definitions."""

from velune.core.types.agent import AgentMessage, AgentMessageType, AgentResult, AgentRole
from velune.core.types.context import ContextChunk, ContextPriority, ContextWindow
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.memory import MemoryQuery, MemoryRecord, MemoryType
from velune.core.types.model import (
    CapabilityLevel,
    ModelCapability,
    ModelCapabilityProfile,
    ModelDescriptor,
)
from velune.core.types.provider import ProviderCapabilities, ProviderConfig
from velune.core.types.repository import DependencyEdge, FileNode, SymbolNode
from velune.core.types.task import Task, TaskPlan, TaskResult, TaskStatus, TaskStep
from velune.core.types.workspace import WorkspaceEvent, WorkspaceState

__all__ = [
    "AgentRole",
    "AgentMessage",
    "AgentMessageType",
    "AgentResult",
    "ContextPriority",
    "ContextChunk",
    "ContextWindow",
    "InferenceRequest",
    "StreamChunk",
    "InferenceResponse",
    "MemoryType",
    "MemoryRecord",
    "MemoryQuery",
    "CapabilityLevel",
    "ModelCapability",
    "ModelCapabilityProfile",
    "ModelDescriptor",
    "ProviderConfig",
    "ProviderCapabilities",
    "FileNode",
    "SymbolNode",
    "DependencyEdge",
    "TaskStatus",
    "Task",
    "TaskStep",
    "TaskPlan",
    "TaskResult",
    "WorkspaceState",
    "WorkspaceEvent",
]
