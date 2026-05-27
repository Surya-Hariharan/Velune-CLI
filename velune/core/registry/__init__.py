"""Global component registry."""

from velune.kernel.registry import (
    ServiceContainer,
    get_container,
    inject,
)

__all__ = [
    "ServiceContainer",
    "inject",
    "get_container",
]
