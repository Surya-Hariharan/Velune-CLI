"""Typed error hierarchy."""

from velune.core.errors.execution import (
    ExecutionError,
    RollbackError,
    SandboxError,
    SnapshotError,
    ValidationError,
)
from velune.core.errors.memory import (
    VeluneMemoryConsolidationError,
    VeluneMemoryError,
    VeluneMemoryRetrievalError,
    VeluneMemoryStoreError,
)
from velune.core.errors.orchestration import (
    AgentExecutionError,
    CheckpointError,
    OrchestrationError,
    PipelineExecutionError,
    StateTransitionError,
)
from velune.core.errors.provider import (
    InferenceError,
    ModelNotFoundError,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderError,
    ProviderNotFoundError,
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
    "VeluneMemoryError",
    "VeluneMemoryStoreError",
    "VeluneMemoryRetrievalError",
    "VeluneMemoryConsolidationError",
    "ExecutionError",
    "SandboxError",
    "SnapshotError",
    "RollbackError",
    "ValidationError",
]
