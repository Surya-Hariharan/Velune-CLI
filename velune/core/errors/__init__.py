"""Typed error hierarchy."""

from velune.core.errors.provider import (
    ProviderError,
    ProviderNotFoundError,
    ProviderConnectionError,
    ProviderAuthenticationError,
    ModelNotFoundError,
    InferenceError,
)
from velune.core.errors.orchestration import (
    OrchestrationError,
    AgentExecutionError,
    PipelineExecutionError,
    StateTransitionError,
    CheckpointError,
)
from velune.core.errors.memory import (
    MemoryError,
    MemoryStoreError,
    MemoryRetrievalError,
    MemoryConsolidationError,
)
from velune.core.errors.execution import (
    ExecutionError,
    SandboxError,
    SnapshotError,
    RollbackError,
    ValidationError,
)

__all__ = [
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
    "MemoryError",
    "MemoryStoreError",
    "MemoryRetrievalError",
    "MemoryConsolidationError",
    "ExecutionError",
    "SandboxError",
    "SnapshotError",
    "RollbackError",
    "ValidationError",
]
