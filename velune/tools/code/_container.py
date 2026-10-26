"""Resolving the service container a code tool reads runtime services from."""

from __future__ import annotations

from typing import Any


def resolve_container(injected: Any | None) -> Any:
    """The injected runtime container, else the process-wide registry.

    The runtime builds its own container; tools are handed it at registration so
    they see the services of *their* runtime. The registry fallback only serves
    standalone construction (tests, ad-hoc scripts).
    """
    if injected is not None:
        return injected
    from velune.kernel.registry import get_container

    return get_container()
