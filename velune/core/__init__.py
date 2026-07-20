"""Foundational primitives and contracts."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from velune.core.errors import *
from velune.kernel.config import (
    ConfigLoader,
    ContextConfig,
    ExecutionConfig,
    MemoryConfig,
    ProjectConfig,
    ProviderEntry,
    ProvidersConfig,
    RetrievalConfig,
    TelemetryConfig,
    VeluneConfig,
    WorkspaceConfig,
    get_default_config,
)
from velune.kernel.registry import ServiceContainer, get_container, inject

if TYPE_CHECKING:
    from velune.core.types import (
        AgentMessage,
        AgentMessageType,
        AgentResult,
        AgentRole,
        CapabilityLevel,
        DependencyEdge,
        FileNode,
        InferenceRequest,
        InferenceResponse,
        MemoryQuery,
        MemoryRecord,
        MemoryType,
        ModelCapability,
        ModelDescriptor,
        ProviderCapabilities,
        ProviderConfig,
        StreamChunk,
        SymbolNode,
        Task,
        TaskPlan,
        TaskResult,
        TaskStatus,
        TaskStep,
        WorkspaceEvent,
        WorkspaceState,
    )

__all__ = [
    # Types
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
    # Config
    "VeluneConfig",
    "ProjectConfig",
    "WorkspaceConfig",
    "ContextConfig",
    "MemoryConfig",
    "RetrievalConfig",
    "ExecutionConfig",
    "ProviderEntry",
    "ProvidersConfig",
    "TelemetryConfig",
    "ConfigLoader",
    "get_default_config",
    # Errors
    "ProviderError",
    "ProviderNotFoundError",
    "ProviderConnectionError",
    "ProviderAuthenticationError",
    "ModelNotFoundError",
    "InferenceError",
    "OrchestrationError",
    "AgentExecutionError",
    "PipelineExecutionError",
    "StateTransitionError",
    "CheckpointError",
    "VeluneMemoryError",
    "VeluneMemoryStoreError",
    "VeluneMemoryRetrievalError",
    "VeluneMemoryConsolidationError",
    "ExecutionError",
    "SandboxError",
    "SnapshotError",
    "RollbackError",
    "ValidationError",
    # Registry
    "ServiceContainer",
    "inject",
    "get_container",
]

# Names re-exported from velune.core.types, resolved lazily on first access
# (see that package's __init__.py for why). Deliberately NOT the full set of
# velune.core.types.__all__: this mirrors the subset this module has always
# exported (e.g. it never re-exported ToolCall or ModelCapabilityProfile) —
# and deliberately excludes ContextPriority/ContextChunk/ContextWindow, which
# are listed in __all__ above but have named no real module for years (that
# source file was removed at some point); `from velune.core import
# ContextChunk` already raised AttributeError before this change, and this
# preserves that rather than silently making it resolve.
_LAZY_TYPE_NAMES: frozenset[str] = frozenset(
    {
        "AgentRole",
        "AgentMessage",
        "AgentMessageType",
        "AgentResult",
        "InferenceRequest",
        "StreamChunk",
        "InferenceResponse",
        "MemoryType",
        "MemoryRecord",
        "MemoryQuery",
        "CapabilityLevel",
        "ModelCapability",
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
    }
)


def __getattr__(name: str) -> Any:
    if name not in _LAZY_TYPE_NAMES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module("velune.core.types"), name)
    globals()[name] = value  # cache: subsequent access skips __getattr__ entirely
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
