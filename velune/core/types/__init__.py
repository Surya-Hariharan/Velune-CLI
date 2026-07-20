"""Core type definitions.

Re-exports are lazy (PEP 562 module ``__getattr__``): importing this package
no longer eagerly compiles all eight sibling modules' pydantic schemas. A
command that only needs ``ModelDescriptor`` (every Tier-0 path, via
``providers/base.py``) used to also pay for ``agent.py``/``task.py``/
``memory.py``/``repository.py``/``workspace.py`` — modules only ever used by
Tier-1 cognition/orchestration code, and which that code already imports
directly (``from velune.core.types.task import TaskPlan``, never through this
package). Only the names actually touched get compiled, on first access.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.core.types.agent import AgentMessage, AgentMessageType, AgentResult, AgentRole
    from velune.core.types.inference import (
        InferenceRequest,
        InferenceResponse,
        StreamChunk,
        ToolCall,
    )
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
    "InferenceRequest",
    "StreamChunk",
    "InferenceResponse",
    "ToolCall",
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

# name -> owning submodule, one entry per __all__ member.
_NAME_TO_MODULE: dict[str, str] = {
    "AgentRole": "velune.core.types.agent",
    "AgentMessage": "velune.core.types.agent",
    "AgentMessageType": "velune.core.types.agent",
    "AgentResult": "velune.core.types.agent",
    "InferenceRequest": "velune.core.types.inference",
    "StreamChunk": "velune.core.types.inference",
    "InferenceResponse": "velune.core.types.inference",
    "ToolCall": "velune.core.types.inference",
    "MemoryType": "velune.core.types.memory",
    "MemoryRecord": "velune.core.types.memory",
    "MemoryQuery": "velune.core.types.memory",
    "CapabilityLevel": "velune.core.types.model",
    "ModelCapability": "velune.core.types.model",
    "ModelCapabilityProfile": "velune.core.types.model",
    "ModelDescriptor": "velune.core.types.model",
    "ProviderConfig": "velune.core.types.provider",
    "ProviderCapabilities": "velune.core.types.provider",
    "FileNode": "velune.core.types.repository",
    "SymbolNode": "velune.core.types.repository",
    "DependencyEdge": "velune.core.types.repository",
    "TaskStatus": "velune.core.types.task",
    "Task": "velune.core.types.task",
    "TaskStep": "velune.core.types.task",
    "TaskPlan": "velune.core.types.task",
    "TaskResult": "velune.core.types.task",
    "WorkspaceState": "velune.core.types.workspace",
    "WorkspaceEvent": "velune.core.types.workspace",
}


def __getattr__(name: str) -> Any:
    module_path = _NAME_TO_MODULE.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value  # cache: subsequent access skips __getattr__ entirely
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
