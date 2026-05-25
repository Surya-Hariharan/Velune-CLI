"""Plugin hook boundaries and dispatch mechanisms."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("velune.plugins.hooks")


class PluginHookDispatcher:
    """Manages subscription and invocation of custom plugin callbacks during execution."""

    def __init__(self) -> None:
        self._hooks: dict[str, list[Callable[..., Any]]] = {
            "pre_execute": [],
            "post_retrieve": [],
            "on_arbitrate": [],
        }

    def register_hook(self, hook_name: str, callback: Callable[..., Any]) -> None:
        """Register a callback for a specific hook boundary."""
        if hook_name in self._hooks:
            self._hooks[hook_name].append(callback)
            logger.info("Registered callback for hook point: %s", hook_name)
        else:
            logger.warning("Attempted to register callback for unknown hook point: %s", hook_name)

    async def trigger(self, hook_name: str, *args: Any, **kwargs: Any) -> list[Any]:
        """Trigger all registered callbacks for a hook name, capturing results."""
        if hook_name not in self._hooks:
            return []

        results = []
        for callback in self._hooks[hook_name]:
            try:
                # Handle both async and sync callbacks cleanly
                import asyncio
                if asyncio.iscoroutinefunction(callback):
                    res = await callback(*args, **kwargs)
                else:
                    res = callback(*args, **kwargs)
                results.append(res)
            except Exception as e:
                logger.error("Error executing callback for hook %s: %s", hook_name, e)
                results.append(None)

        return results
