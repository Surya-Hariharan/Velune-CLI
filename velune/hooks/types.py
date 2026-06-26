"""Data types for the Velune hook system."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from velune._compat import StrEnum


class HookEvent(StrEnum):
    """All lifecycle events that hooks can subscribe to."""

    SESSION_START = "SessionStart"
    PRE_TOOL_USE = "PreToolUse"
    POST_TOOL_USE = "PostToolUse"
    USER_PROMPT_SUBMIT = "UserPromptSubmit"
    STOP = "Stop"
    SUBAGENT_STOP = "SubagentStop"
    MESSAGE_DISPLAY = "MessageDisplay"


@dataclass
class HookCondition:
    """Optional `if` guard on a hook entry.

    Format: ``Bash(rm -rf*)``, ``Edit(src/**)`` or plain ``*``.

    Attributes:
        tool:    Tool name to match — ``"Bash"``, ``"Edit|Write"``, ``"*"``.
        pattern: Glob pattern matched against the tool's primary argument
                 (command for Bash, file_path for filesystem tools).
    """

    tool: str = "*"
    pattern: str = "*"


@dataclass
class HookDefinition:
    """A single executable hook entry.

    Attributes:
        type:      Always ``"command"`` for now (future: ``"python"``, ``"inline"``).
        command:   Shell command executed as a subprocess; receives event JSON on stdin,
                   must write a JSON response to stdout.
        timeout:   Seconds before the subprocess is killed (default 10).
        condition: Optional ``if`` guard — hook runs only when the guard matches.
        env:       Extra environment variables injected into the subprocess.
    """

    type: str
    command: str
    timeout: int = 10
    condition: HookCondition | None = None
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class HookBinding:
    """Maps a hook event to a list of hook definitions.

    Attributes:
        event:   The lifecycle event to bind to.
        hooks:   Ordered list of hook definitions to run.
        matcher: Tool-name filter for PreToolUse/PostToolUse
                 (``"Bash"``, ``"Edit|Write"``, ``"*"``).
    """

    event: HookEvent
    hooks: list[HookDefinition]
    matcher: str = "*"


@dataclass
class HookResult:
    """Aggregated result returned after running all hooks for one event.

    Attributes:
        blocked:             True if any hook returned ``decision: "block"``.
        block_reason:        Human-readable reason shown in the UI.
        system_message:      Informational message injected above the next response.
        additional_context:  Extra text appended to the conversation (Stop hooks).
        session_title:       Session title override (SessionStart hooks).
        transformed_prompt:  Replacement user prompt (UserPromptSubmit hooks).
        transformed_message: Replacement assistant output (MessageDisplay hooks).
        reload_skills:       True if any hook requested a skill hot-reload.
        raw_outputs:         Raw JSON response dicts from every hook subprocess.
    """

    blocked: bool = False
    block_reason: str = ""
    system_message: str = ""
    additional_context: str = ""
    session_title: str = ""
    transformed_prompt: str = ""
    transformed_message: str = ""
    reload_skills: bool = False
    raw_outputs: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def merge(cls, results: list[HookResult]) -> HookResult:
        """Merge multiple results from parallel hook commands into one."""
        merged = cls()
        for r in results:
            merged.raw_outputs.extend(r.raw_outputs)
            if r.blocked:
                merged.blocked = True
                merged.block_reason = f"{merged.block_reason}\n{r.block_reason}".strip()
            if r.system_message:
                merged.system_message = f"{merged.system_message}\n{r.system_message}".strip()
            if r.additional_context:
                merged.additional_context = (
                    f"{merged.additional_context}\n{r.additional_context}".strip()
                )
            if r.session_title:
                merged.session_title = r.session_title
            if r.transformed_prompt:
                merged.transformed_prompt = r.transformed_prompt
            if r.transformed_message:
                merged.transformed_message = r.transformed_message
            if r.reload_skills:
                merged.reload_skills = True
        return merged

    @classmethod
    def from_subprocess_output(cls, raw: dict[str, Any]) -> HookResult:
        """Parse the JSON dict returned by a hook subprocess into a HookResult."""
        result = cls(raw_outputs=[raw])

        decision = raw.get("decision", "")
        if decision == "block":
            result.blocked = True
            result.block_reason = raw.get("reason", raw.get("systemMessage", ""))

        result.system_message = raw.get("systemMessage", "")

        specific = raw.get("hookSpecificOutput", {})
        result.session_title = specific.get("sessionTitle", "")
        result.transformed_prompt = specific.get("transformedPrompt", "")
        result.transformed_message = specific.get("transformedMessage", "")
        result.additional_context = specific.get("additionalContext", "")
        result.reload_skills = bool(specific.get("reloadSkills", False))

        return result
