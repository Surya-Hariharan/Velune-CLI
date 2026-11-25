"""Foundational primitives and contracts."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from velune.core.errors import *
from velune.kernel.config import (
    ConfigLoader,
    ContextConfig,
    ExecutionConfig,
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
        AgentMessageType,
        AgentRole,
        CapabilityLevel,
        InferenceRequest,
        InferenceResponse,
        MemoryRecord,
        MemoryType,
        ModelDescriptor,
        ProviderCapabilities,
        StreamChunk,
        Task,
        TaskPlan,
        TaskResult,
        TaskStatus,
        TaskStep,
    )

__all__ = [
    # Types
    "AgentRole",
    "AgentMessageType",
    "InferenceRequest",
    "StreamChunk",
    "InferenceResponse",
    "MemoryType",
    "MemoryRecord",
    "CapabilityLevel",
    "ModelDescriptor",
    "ProviderCapabilities",
    "TaskStatus",
    "Task",
    "TaskStep",
    "TaskPlan",
    "TaskResult",
    # Config
    "VeluneConfig",
    "ProjectConfig",
    "WorkspaceConfig",
    "ContextConfig",
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
    "VeluneMemoryError",
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
        "AgentMessageType",
        "InferenceRequest",
        "StreamChunk",
        "InferenceResponse",
        "MemoryType",
        "MemoryRecord",
        "CapabilityLevel",
        "ModelDescriptor",
        "ProviderCapabilities",
        "TaskStatus",
        "Task",
        "TaskStep",
        "TaskPlan",
        "TaskResult",
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
