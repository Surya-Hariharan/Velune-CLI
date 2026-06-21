"""Typed error hierarchy."""

from velune.core.errors.catalog import (
    APIKeyMissingError,
    ContextWindowExceededError,
    IndexingFailedError,
    InsufficientVRAMError,
    NoModelsAvailableError,
    OllamaNotRunningError,
    ProviderUnavailableError,
    RateLimitError,
    SSRFAttemptError,
    VeluneError,
    WorkspaceNotInitializedError,
)
from velune.core.errors.catalog import (
    ModelNotFoundError as ModelNotFoundVeluneError,
)
from velune.core.errors.catalog import (
    PathTraversalError as PathTraversalVeluneError,
)
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
    # User-facing catalog errors
    "VeluneError",
    "OllamaNotRunningError",
    "ModelNotFoundVeluneError",
    "NoModelsAvailableError",
    "APIKeyMissingError",
    "WorkspaceNotInitializedError",
    "ProviderUnavailableError",
    "ContextWindowExceededError",
    "RateLimitError",
    "InsufficientVRAMError",
    "PathTraversalVeluneError",
    "SSRFAttemptError",
    "IndexingFailedError",
    # Internal provider errors
    "ProviderError",
    "ProviderNotFoundError",
    "ProviderConnectionError",
    "ProviderAuthenticationError",
    "ModelNotFoundError",
    "InferenceError",
    # Internal orchestration errors
    "OrchestrationError",
    "AgentExecutionError",
    "PipelineExecutionError",
    "StateTransitionError",
    "CheckpointError",
    # Internal memory errors
    "VeluneMemoryError",
    "VeluneMemoryStoreError",
    "VeluneMemoryRetrievalError",
    "VeluneMemoryConsolidationError",
    # Internal execution errors
    "ExecutionError",
    "SandboxError",
    "SnapshotError",
    "RollbackError",
    "ValidationError",
]
