"""Global component registry."""

from velune.core.registry.bindings import register_default_bindings
from velune.kernel.registry import (
    ServiceContainer,
    get_container,
    inject,
)

__all__ = [
    "ServiceContainer",
    "inject",
    "get_container",
    "register_default_bindings",
]
