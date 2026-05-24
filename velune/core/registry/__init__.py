"""Global component registry."""

from velune.kernel.registry import (
    ServiceContainer,
    inject,
    get_container,
)
from velune.core.registry.bindings import register_default_bindings

__all__ = [
    "ServiceContainer",
    "inject",
    "get_container",
    "register_default_bindings",
]
