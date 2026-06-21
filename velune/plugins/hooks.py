"""Plugin hook boundaries and dispatch mechanisms.

In-process callbacks for plugin authors sit here.  For *external* subprocess
hooks (the lifecycle system), use ``velune.hooks.HookDispatcher`` directly.

When a ``HookDispatcher`` is attached via ``set_hook_dispatcher()``, the legacy
``pre_execute`` and ``post_retrieve`` trigger points automatically bridge to the
external lifecycle system (PreToolUse / PostToolUse) in addition to firing any
in-process callbacks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from velune.hooks import HookDispatcher

logger = logging.getLogger("velune.plugins.hooks")

# Legacy hook names mapped to the lifecycle system equivalents
_LIFECYCLE_MAP: dict[str, str] = {
    "pre_execute": "PreToolUse",
    "post_retrieve": "PostToolUse",
}


class PluginHookDispatcher:
    """Manages subscription and invocation of custom plugin callbacks during execution.

    Legacy in-process hook system for Velune plugins.  Also bridges to the
    external ``HookDispatcher`` (subprocess lifecycle hooks) when one is
    attached, so plugins and external hooks share the same trigger points.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[Callable[..., Any]]] = {
            "pre_execute": [],
            "post_retrieve": [],
            "on_arbitrate": [],
        }
        self._lifecycle: HookDispatcher | None = None

    # ------------------------------------------------------------------
    # Bridge to external lifecycle dispatcher
    # ------------------------------------------------------------------

    def set_hook_dispatcher(self, dispatcher: HookDispatcher) -> None:
        """Attach the session-level HookDispatcher for lifecycle bridging.

        After attaching, calls to ``trigger("pre_execute", ...)`` will also
        fire the external PreToolUse hooks, and ``trigger("post_retrieve", ...)``
        will fire PostToolUse hooks.
        """
        self._lifecycle = dispatcher
        logger.debug("Lifecycle HookDispatcher attached to PluginHookDispatcher")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_hook(self, hook_name: str, callback: Callable[..., Any]) -> None:
        """Register an in-process callback for a specific hook boundary."""
        if hook_name in self._hooks:
            self._hooks[hook_name].append(callback)
            logger.info("Registered callback for hook point: %s", hook_name)
        else:
            logger.warning("Attempted to register callback for unknown hook point: %s", hook_name)

    # ------------------------------------------------------------------
    # Trigger
    # ------------------------------------------------------------------

    async def trigger(self, hook_name: str, *args: Any, **kwargs: Any) -> list[Any]:
        """Trigger all registered callbacks for a hook name.

        When a ``HookDispatcher`` is attached and this hook name has a lifecycle
        equivalent, the external hooks are fired concurrently with the in-process
        callbacks and their results are merged into the return list.

        Args:
            hook_name: One of ``"pre_execute"``, ``"post_retrieve"``, or
                       ``"on_arbitrate"``.
            *args:     Positional arguments forwarded to in-process callbacks.
            **kwargs:  Keyword arguments forwarded to in-process callbacks.

        Returns:
            List of results from all in-process callbacks (lifecycle hook results
            are included only when they carry a non-empty payload).
        """
        if hook_name not in self._hooks:
            return []

        # ── In-process callbacks ──────────────────────────────────────
        results: list[Any] = []
        for callback in self._hooks[hook_name]:
            try:
                if asyncio.iscoroutinefunction(callback):
                    res = await callback(*args, **kwargs)
                else:
                    res = callback(*args, **kwargs)
                results.append(res)
            except Exception as exc:
                logger.error("Error executing callback for hook %s: %s", hook_name, exc)
                results.append(None)

        # ── Lifecycle bridge ──────────────────────────────────────────
        if self._lifecycle is not None and hook_name in _LIFECYCLE_MAP:
            try:
                lifecycle_result = await self._bridge_to_lifecycle(hook_name, args, kwargs)
                if lifecycle_result is not None:
                    results.append(lifecycle_result)
            except Exception as exc:
                logger.debug("Lifecycle bridge error for %s (non-fatal): %s", hook_name, exc)

        return results

    async def _bridge_to_lifecycle(
        self,
        hook_name: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any | None:
        """Map a legacy hook call to the external HookDispatcher."""
        assert self._lifecycle is not None

        # Extract tool name and input from common call signatures.
        # Plugins typically call: trigger("pre_execute", tool_name, tool_input)
        tool_name: str = ""
        tool_input: dict[str, Any] = {}

        if args:
            first = args[0]
            if isinstance(first, str):
                tool_name = first
            elif isinstance(first, dict):
                tool_input = first

        if len(args) >= 2 and isinstance(args[1], dict):
            tool_input = args[1]

        tool_name = tool_name or kwargs.get("tool_name", "") or kwargs.get("name", "")
        tool_input = tool_input or kwargs.get("tool_input", {}) or kwargs.get("input", {})

        if hook_name == "pre_execute":
            result = await self._lifecycle.dispatch_pre_tool_use(
                tool_name=tool_name or "unknown",
                tool_input=tool_input,
            )
            if result.blocked or result.system_message:
                return result
            return None

        if hook_name == "post_retrieve":
            tool_result = kwargs.get("result") or (args[2] if len(args) > 2 else None)
            await self._lifecycle.dispatch_post_tool_use(
                tool_name=tool_name or "unknown",
                tool_input=tool_input,
                tool_result=tool_result,
            )
            return None

        return None
