"""Typed error hierarchy."""

from velune.core.errors.catalog import (
    ModelNotFoundError as ModelNotFoundVeluneError,
)
from velune.core.errors.catalog import (
    NoModelsAvailableError,
    RateLimitError,
    VeluneError,
    WorkspaceNotInitializedError,
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
    VeluneMemoryError,
)
from velune.core.errors.orchestration import (
    OrchestrationError,
)
from velune.core.errors.provider import (
    InferenceError,
    ModelNotFoundError,
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderError,
    ProviderNotFoundError,
    ProviderPermissionError,
    StreamInterruptedError,
)

__all__ = [
    # User-facing catalog errors
    "VeluneError",
    "ModelNotFoundVeluneError",
    "NoModelsAvailableError",
    "WorkspaceNotInitializedError",
    "RateLimitError",
    "PathTraversalVeluneError",
    # Internal provider errors
    "ProviderError",
    "ProviderNotFoundError",
    "ProviderConnectionError",
    "ProviderAuthenticationError",
    "ProviderPermissionError",
    "ModelNotFoundError",
    "InferenceError",
    "StreamInterruptedError",
    # Internal orchestration errors
    "OrchestrationError",
    # Internal memory errors
    "VeluneMemoryError",
    # Internal execution errors
    "ExecutionError",
    "SandboxError",
    "SnapshotError",
    "RollbackError",
    "ValidationError",
]
