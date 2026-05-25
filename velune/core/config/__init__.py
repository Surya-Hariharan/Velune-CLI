"""Configuration management."""

from __future__ import annotations

import warnings
from typing import Any

__all__ = [
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
    "MCPConfig",
    "ConfigLoader",
    "get_default_config",
]


def __getattr__(name: str) -> Any:
    deprecated_names = {
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
        "MCPConfig",
        "get_default_config",
    }

    if name in deprecated_names:
        warnings.warn(
            f"Importing {name} from velune.core.config is deprecated. "
            f"Use 'from velune.kernel.config import {name}' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        import velune.kernel.config
        return getattr(velune.kernel.config, name)

    if name == "ConfigLoader":
        from velune.core.config.loader import ConfigLoader
        return ConfigLoader

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
