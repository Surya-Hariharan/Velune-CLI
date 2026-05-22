"""Plugin sandbox boundary wrapper enforcing execution safety and capturing errors."""

from __future__ import annotations

from typing import Any, Callable
import logging

logger = logging.getLogger("velune.plugins.sandbox")


class PluginSandbox:
    """Wraps plugin interactions to prevent unhandled exceptions from crashing the primary process."""

    @staticmethod
    def wrap_callback(callback: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap callback to run inside try-except boundary, catching all leakage or crashes."""
        
        # Handle async callbacks
        import asyncio
        if asyncio.iscoroutinefunction(callback):
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    logger.debug("Sandbox executing async plugin callback: %s", callback.__name__)
                    return await callback(*args, **kwargs)
                except Exception as e:
                    logger.error(
                        "Plugin sandbox intercepted crash in callback %s: %s",
                        callback.__name__,
                        e,
                    )
                    return None
            return async_wrapper
        else:
            # Handle sync callbacks
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                try:
                    logger.debug("Sandbox executing sync plugin callback: %s", callback.__name__)
                    return callback(*args, **kwargs)
                except Exception as e:
                    logger.error(
                        "Plugin sandbox intercepted crash in callback %s: %s",
                        callback.__name__,
                        e,
                    )
                    return None
            return sync_wrapper
class PluginExecutionSandbox:
    """Safety environment manager for general plugin context executions."""
    pass
