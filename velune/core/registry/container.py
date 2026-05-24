"""Backward-compatible re-export. Import from velune.kernel.registry directly."""

import warnings

warnings.warn(
    "Import from velune.kernel.registry directly. velune.core.registry.container is deprecated.",
    DeprecationWarning,
    stacklevel=2,
)

from velune.kernel.registry import ServiceContainer, inject, get_container

__all__ = ["ServiceContainer", "inject", "get_container"]
