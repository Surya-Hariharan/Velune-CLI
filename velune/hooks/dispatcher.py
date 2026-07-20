"""Main HookDispatcher — the single entry point for all hook events.

Usage::

    dispatcher = HookDispatcher(workspace=Path("/my/project"))

    # On session start
    result = await dispatcher.dispatch_session_start(session_id, model_id)
    if result.session_title:
        use_title(result.session_title)

    # Before a tool runs
    result = await dispatcher.dispatch_pre_tool_use("Bash", {"command": "rm -rf /tmp"})
    if result.blocked:
        raise ToolBlockedByHookError(result.block_reason)

    # After a tool runs
    await dispatcher.dispatch_post_tool_use("Bash", input_dict, result_dict)

    # When user submits a prompt
    result = await dispatcher.dispatch_user_prompt(user_text)
    if result.transformed_prompt:
        user_text = result.transformed_prompt

    # Before displaying an assistant message
    result = await dispatcher.dispatch_message_display(message_text, "assistant")
    display_text = result.transformed_message or message_text

    # On session end
    await dispatcher.dispatch_stop(reason="normal_exit", transcript_path=str(path))
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from velune.hooks.config import load_hooks
from velune.hooks.executor import (
    build_message_display_payload,
    build_post_tool_payload,
    build_pre_tool_payload,
    build_session_start_payload,
    build_stop_payload,
    build_user_prompt_payload,
    run_hooks_parallel,
)
from velune.hooks.matcher import condition_matches, tool_matcher_matches
from velune.hooks.types import HookBinding, HookDefinition, HookEvent, HookResult

logger = logging.getLogger("velune.hooks.dispatcher")


class HookDispatcher:
    """Coordinates hook loading, filtering, and execution for all event types.

    The dispatcher is created once per session (usually inside VeluneREPL) and
    lazily loads hook configurations on first use.  Reload by calling
    ``invalidate_cache()`` after config changes.

    Attributes:
        workspace:  Active project root (determines which hooks.json to read).
        session_id: Unique identifier for the current REPL session.
    """

    def __init__(
        self,
        workspace: Path | None = None,
        session_id: str | None = None,
        trusted: bool = False,
    ) -> None:
        self.workspace = workspace
        self.session_id = session_id or str(uuid.uuid4())[:8]
        self._trusted = trusted
        self._bindings: list[HookBinding] | None = None  # lazy-loaded

    @property
    def trusted(self) -> bool:
        """Whether project-level hooks are permitted for this workspace."""
        return self._trusted

    def set_trusted(self, trusted: bool) -> None:
        """Record the workspace trust decision and drop any cached bindings.

        Must be called before the first dispatch: bindings are loaded lazily and
        cached, so a trust decision that arrives after loading would not take
        effect without the invalidation below.
        """
        if trusted != self._trusted:
            self._trusted = trusted
            self.invalidate_cache()

    # ------------------------------------------------------------------
    # Cache management
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> list[HookBinding]:
        if self._bindings is None:
            self._bindings = load_hooks(self.workspace, trusted=self._trusted)
            logger.debug(
                "Loaded %d hook binding(s) for workspace %s",
                len(self._bindings),
                self.workspace,
            )
        return self._bindings

    def invalidate_cache(self) -> None:
        """Force-reload hooks from disk on the next dispatch call."""
        self._bindings = None

    @property
    def has_hooks(self) -> bool:
        """True if at least one hook binding is configured."""
        return bool(self._ensure_loaded())

    def bindings_for(self, event: HookEvent) -> list[HookBinding]:
        """Return all bindings registered for a specific event."""
        return [b for b in self._ensure_loaded() if b.event == event]

    # ------------------------------------------------------------------
    # Internal dispatch helper
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        event: HookEvent,
        payload: dict[str, Any],
        tool_name: str = "",
        tool_input: dict[str, Any] | None = None,
    ) -> HookResult:
        """Select matching hook definitions and run them, returning merged result."""
        bindings = self.bindings_for(event)
        if not bindings:
            return HookResult()

        matching_defs: list[HookDefinition] = []
        for binding in bindings:
            # Coarse matcher filter for tool-related events
            if tool_name and not tool_matcher_matches(binding.matcher, tool_name):
                continue

            for defn in binding.hooks:
                # Fine-grained if-condition filter
                if defn.condition and tool_name and tool_input is not None:
                    if not condition_matches(defn.condition, tool_name, tool_input):
                        continue
                matching_defs.append(defn)

        if not matching_defs:
            return HookResult()

        result = await run_hooks_parallel(
            matching_defs,
            payload,
            workspace=self.workspace,
        )

        # Log blocks and messages so users can see why something was denied
        if result.blocked:
            logger.info("[hook] Blocked by %s hook: %s", event.value, result.block_reason)
        if result.system_message:
            logger.debug("[hook] system_message from %s: %s", event.value, result.system_message)

        return result

    # ------------------------------------------------------------------
    # Public dispatch methods — one per event type
    # ------------------------------------------------------------------

    async def dispatch_session_start(
        self,
        session_id: str | None = None,
        model_id: str = "",
    ) -> HookResult:
        """Fire the SessionStart event.

        Returns a HookResult that may carry:
        - ``session_title``: suggested title for the session
        - ``system_message``: startup notice injected into the UI
        - ``reload_skills``: signal to re-scan skill directories
        """
        payload = build_session_start_payload(
            session_id=session_id or self.session_id,
            workspace=str(self.workspace or ""),
            model_id=model_id,
        )
        return await self._dispatch(HookEvent.SESSION_START, payload)

    async def dispatch_pre_tool_use(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        session_id: str | None = None,
    ) -> HookResult:
        """Fire the PreToolUse event before a tool executes.

        Returns a HookResult that may carry:
        - ``blocked``: True → deny the tool call and show ``block_reason``
        - ``system_message``: informational warning shown to the user
        """
        payload = build_pre_tool_payload(
            tool_name=tool_name,
            tool_input=tool_input,
            session_id=session_id or self.session_id,
            workspace=str(self.workspace or ""),
        )
        return await self._dispatch(
            HookEvent.PRE_TOOL_USE,
            payload,
            tool_name=tool_name,
            tool_input=tool_input,
        )

    async def dispatch_post_tool_use(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: Any,
        session_id: str | None = None,
    ) -> HookResult:
        """Fire the PostToolUse event after a tool returns.

        Returns a HookResult (usually empty; used for auditing/logging hooks).
        """
        payload = build_post_tool_payload(
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
            session_id=session_id or self.session_id,
            workspace=str(self.workspace or ""),
        )
        return await self._dispatch(
            HookEvent.POST_TOOL_USE,
            payload,
            tool_name=tool_name,
            tool_input=tool_input,
        )

    async def dispatch_user_prompt(
        self,
        user_prompt: str,
        session_id: str | None = None,
    ) -> HookResult:
        """Fire the UserPromptSubmit event when the user presses Enter.

        Returns a HookResult that may carry:
        - ``blocked``: True → prevent the prompt from being sent
        - ``transformed_prompt``: replacement text for the user message
        - ``system_message``: prefix message injected before model call
        """
        payload = build_user_prompt_payload(
            user_prompt=user_prompt,
            session_id=session_id or self.session_id,
            workspace=str(self.workspace or ""),
        )
        return await self._dispatch(HookEvent.USER_PROMPT_SUBMIT, payload)

    async def dispatch_stop(
        self,
        reason: str = "normal_exit",
        transcript_path: str = "",
        session_id: str | None = None,
    ) -> HookResult:
        """Fire the Stop event when the session is about to end.

        Returns a HookResult that may carry:
        - ``blocked``: True → prevent exit (re-prompts the user)
        - ``additional_context``: text injected as a final assistant message
        """
        payload = build_stop_payload(
            session_id=session_id or self.session_id,
            workspace=str(self.workspace or ""),
            reason=reason,
            transcript_path=transcript_path,
        )
        return await self._dispatch(HookEvent.STOP, payload)

    async def dispatch_subagent_stop(
        self,
        reason: str = "normal_exit",
        session_id: str | None = None,
    ) -> HookResult:
        """Fire the SubagentStop event when a council agent finishes."""
        payload = {
            "hook_event_name": "SubagentStop",
            "reason": reason,
            "session_id": session_id or self.session_id,
            "workspace": str(self.workspace or ""),
        }
        return await self._dispatch(HookEvent.SUBAGENT_STOP, payload)

    async def dispatch_message_display(
        self,
        message: str,
        role: str = "assistant",
        session_id: str | None = None,
    ) -> HookResult:
        """Fire the MessageDisplay event before rendering an assistant message.

        Returns a HookResult that may carry:
        - ``transformed_message``: replacement content to render instead
        - ``system_message``: annotation injected below the message
        """
        payload = build_message_display_payload(
            message=message,
            role=role,
            session_id=session_id or self.session_id,
        )
        return await self._dispatch(HookEvent.MESSAGE_DISPLAY, payload)

    # ------------------------------------------------------------------
    # Introspection helpers (used by /hooks command)
    # ------------------------------------------------------------------

    def summary(self) -> list[dict]:
        """Return a list of dicts describing all loaded hook bindings."""
        bindings = self._ensure_loaded()
        rows = []
        for b in bindings:
            for defn in b.hooks:
                rows.append(
                    {
                        "event": b.event.value,
                        "matcher": b.matcher,
                        "command": defn.command,
                        "timeout": defn.timeout,
                        "if": (
                            f"{defn.condition.tool}({defn.condition.pattern})"
                            if defn.condition and defn.condition.pattern != "*"
                            else defn.condition.tool
                            if defn.condition
                            else ""
                        ),
                    }
                )
        return rows
