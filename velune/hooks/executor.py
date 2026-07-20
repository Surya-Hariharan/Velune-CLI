"""Hook subprocess executor.

Runs hook commands as external processes, passes event data via JSON stdin,
and parses the JSON response from stdout.

Design principles:
- Hooks ALWAYS exit 0 on error — a crashing hook must never block the user.
- Stdout is the result channel; stderr is the error/debug channel (logged only).
- Hard timeout kills the subprocess so a hung hook can't freeze the REPL.
- The hook receives a rich event payload and may return structured directives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
from pathlib import Path
from typing import Any

from velune.hooks.types import HookDefinition, HookResult

logger = logging.getLogger("velune.hooks.executor")

# Hard ceiling regardless of what the hook config requests
MAX_TIMEOUT_SECONDS = 60


async def run_hook(
    defn: HookDefinition,
    payload: dict[str, Any],
    workspace: Path | None = None,
    env_overrides: dict[str, str] | None = None,
) -> HookResult:
    """Execute a single hook command and return its HookResult.

    Args:
        defn:          The hook definition (command, timeout, env).
        payload:       JSON payload sent to the hook on stdin.
        workspace:     Working directory for the subprocess.
        env_overrides: Extra env vars merged on top of the hook's own ``env``.

    Returns:
        HookResult parsed from stdout. On any failure the result is a no-op
        (no decision, empty messages) — hooks never cascade failures.
    """
    if defn.type != "command":
        logger.warning("Unsupported hook type '%s' — only 'command' is supported", defn.type)
        return HookResult()

    timeout = min(defn.timeout, MAX_TIMEOUT_SECONDS)
    stdin_data = json.dumps(payload).encode("utf-8")

    # Build subprocess environment
    proc_env = os.environ.copy()
    if workspace:
        proc_env["VELUNE_WORKSPACE"] = str(workspace)
    proc_env.update(defn.env)
    if env_overrides:
        proc_env.update(env_overrides)

    # On Windows, delegate command parsing to the OS shell so PATH resolution
    # and quoting work as the user expects. On POSIX, split manually to avoid
    # unintended shell expansion.
    cmd = defn.command
    cwd = str(workspace) if workspace and workspace.is_dir() else None

    try:
        if sys.platform == "win32":
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=proc_env,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *shlex.split(cmd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=proc_env,
            )
    except FileNotFoundError:
        logger.warning("Hook command not found: %s", cmd)
        return HookResult()
    except OSError as exc:
        logger.warning("Hook command failed to start (%s): %s", cmd, exc)
        return HookResult()

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=stdin_data),
            timeout=timeout,
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        # kill() only sends the signal. Without the wait() the child is never
        # reaped and its pipes stay open, leaving a zombie plus an asyncio
        # transport warning behind on every hook timeout.
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception as exc:
            logger.debug("Hook process did not exit after kill (%s): %s", cmd, exc)
        logger.warning("Hook timed out after %ds: %s", timeout, cmd)
        return HookResult()
    except Exception as exc:
        logger.warning("Hook communication error (%s): %s", cmd, exc)
        return HookResult()

    if stderr_bytes:
        for line in stderr_bytes.decode(errors="replace").splitlines():
            logger.debug("[hook stderr] %s", line)

    if not stdout_bytes.strip():
        # Empty stdout is fine — hook ran but had nothing to say
        return HookResult()

    try:
        raw = json.loads(stdout_bytes.decode(errors="replace"))
    except json.JSONDecodeError as exc:
        logger.warning("Hook returned invalid JSON (%s): %s", cmd, exc)
        return HookResult()

    if not isinstance(raw, dict):
        logger.warning("Hook returned non-dict JSON (%s): %s", cmd, type(raw).__name__)
        return HookResult()

    return HookResult.from_subprocess_output(raw)


async def run_hooks_parallel(
    definitions: list[HookDefinition],
    payload: dict[str, Any],
    workspace: Path | None = None,
) -> HookResult:
    """Run multiple hook definitions concurrently and merge their results.

    All hooks are launched in parallel; the merged result reflects the union
    of all their directives (blocking wins if any hook blocks).
    """
    if not definitions:
        return HookResult()

    tasks = [run_hook(defn, payload, workspace=workspace) for defn in definitions]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    valid: list[HookResult] = []
    for r in results:
        if isinstance(r, Exception):
            logger.warning("Hook task raised unexpectedly: %s", r)
        elif isinstance(r, HookResult):
            valid.append(r)

    return HookResult.merge(valid)


def build_pre_tool_payload(
    tool_name: str,
    tool_input: dict[str, Any],
    session_id: str,
    workspace: str = "",
) -> dict[str, Any]:
    """Build the JSON payload for PreToolUse events."""
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
        "workspace": workspace,
    }


def build_post_tool_payload(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_result: Any,
    session_id: str,
    workspace: str = "",
) -> dict[str, Any]:
    """Build the JSON payload for PostToolUse events."""
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_result": tool_result
        if isinstance(tool_result, dict | list | str | int | float | bool)
        else str(tool_result),
        "session_id": session_id,
        "workspace": workspace,
    }


def build_session_start_payload(
    session_id: str,
    workspace: str = "",
    model_id: str = "",
) -> dict[str, Any]:
    """Build the JSON payload for SessionStart events."""
    return {
        "hook_event_name": "SessionStart",
        "session_id": session_id,
        "workspace": workspace,
        "model_id": model_id,
    }


def build_user_prompt_payload(
    user_prompt: str,
    session_id: str,
    workspace: str = "",
) -> dict[str, Any]:
    """Build the JSON payload for UserPromptSubmit events."""
    return {
        "hook_event_name": "UserPromptSubmit",
        "user_prompt": user_prompt,
        "session_id": session_id,
        "workspace": workspace,
    }


def build_stop_payload(
    session_id: str,
    workspace: str = "",
    reason: str = "normal_exit",
    transcript_path: str = "",
) -> dict[str, Any]:
    """Build the JSON payload for Stop events."""
    return {
        "hook_event_name": "Stop",
        "reason": reason,
        "session_id": session_id,
        "workspace": workspace,
        "transcript_path": transcript_path,
    }


def build_message_display_payload(
    message: str,
    role: str,
    session_id: str,
) -> dict[str, Any]:
    """Build the JSON payload for MessageDisplay events."""
    return {
        "hook_event_name": "MessageDisplay",
        "message": message,
        "role": role,
        "session_id": session_id,
    }
