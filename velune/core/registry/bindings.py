"""Default service bindings."""

from velune.kernel.registry import get_container


def register_default_bindings() -> None:
    """Register default service bindings."""
    container = get_container()

    # Configuration will be registered during initialization
    # Provider registry will be registered during initialization
    # Model registry will be registered during initialization
    # Memory stores will be registered during initialization
    # Event bus will be registered during initialization

    # Additional default bindings can be added here
    pass
