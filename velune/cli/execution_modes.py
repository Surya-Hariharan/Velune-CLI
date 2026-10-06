"""Execution mode (MANUAL / PLAN / AUTO): who may change what, from the user's side.

Separate from the fast/normal/max *session* modes (velune/cli/modes.py), which
pick model tier and depth. This module owns the startup value, switching
(``/manual`` ``/plan`` ``/auto``, Shift+Tab), and the message shown on each
switch. Enforcement lives in velune.permissions and the tool layer.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from velune.permissions import ExecutionMode

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

# What each mode means, shown when switching into it.
MODE_DESCRIPTIONS: dict[ExecutionMode, list[str]] = {
    ExecutionMode.MANUAL: [
        "Velune reads and analyzes freely, and asks before every change",
        "(files, folders, commands that change state).",
    ],
    ExecutionMode.PLAN: [
        "Velune investigates and writes a plan to .velune/plans/ — no project files",
        "change while planning. Revise it as often as you like; it runs only after",
        "you approve it (e.g. 'approve' or 'go ahead').",
    ],
    ExecutionMode.AUTO: [
        "Velune can modify files and run allowed commands inside the workspace",
        "without asking at every step.",
    ],
}

# The hard limits of each mode, shown after the description.
MODE_LIMITS: dict[ExecutionMode, list[str]] = {
    ExecutionMode.MANUAL: [
        "Anything outside the workspace asks first.",
    ],
    ExecutionMode.PLAN: [
        "Only the files the approved plan lists may change; anything else asks first.",
    ],
    ExecutionMode.AUTO: [
        "Nothing outside the workspace is changed — switch to MANUAL to allow a location.",
        "High-risk commands, secret files and network access still ask first.",
    ],
}

# One line for the transient Shift+Tab confirmation.
MODE_HINTS: dict[ExecutionMode, str] = {
    ExecutionMode.MANUAL: "asks before every change",
    ExecutionMode.PLAN: "plans first, runs only after approval",
    ExecutionMode.AUTO: "edits inside the workspace without asking",
}

# The same shape for all three: a glyph, then the name.
BADGES: dict[ExecutionMode, str] = {
    ExecutionMode.MANUAL: "● MANUAL",
    ExecutionMode.PLAN: "⏸ PLAN",
    ExecutionMode.AUTO: "⏵⏵ AUTO",
}

NOTICE_SECONDS = 3.0


def parse_mode(value: Any) -> ExecutionMode | None:
    """Parse a mode name (also the legacy /approve values ask/safe/block)."""
    if isinstance(value, ExecutionMode):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    legacy = {"ask": ExecutionMode.MANUAL, "safe": ExecutionMode.AUTO, "block": ExecutionMode.PLAN}
    if text in legacy:
        return legacy[text]
    try:
        return ExecutionMode(text)
    except ValueError:
        return None


def initial_mode(container: Any, config: Any) -> ExecutionMode:
    """``--mode`` (or ``--yes`` → AUTO), else ``[execution] mode``, else MANUAL."""
    try:
        if container is not None and container.has("runtime.execution_mode"):
            mode = parse_mode(container.get("runtime.execution_mode"))
            if mode is not None:
                return mode
    except Exception:
        pass
    configured = getattr(getattr(config, "execution", None), "mode", None)
    return parse_mode(configured) or ExecutionMode.MANUAL


def mode_badge(repl: Any) -> str:
    """The status-bar label: the mode, plus the pending plan's state when there is one."""
    mode = getattr(repl, "_execution_mode", ExecutionMode.MANUAL)
    badge = BADGES[mode]
    plans = getattr(repl, "_plan_manager", None)
    state = plans.state_label() if plans is not None else None
    if state is None:
        return badge
    # Outside PLAN the plan is not running, whatever its file says.
    if mode is ExecutionMode.PLAN:
        return f"{badge} · {state}"
    return f"{badge} · plan pending"


def _sync_policy(repl: Any, mode: ExecutionMode) -> None:
    """Point the live permission state at *mode* now, not at the next turn."""
    gate = getattr(repl, "_permission_gate", None)
    if gate is None:
        return
    gate.state.mode = mode
    plans = getattr(repl, "_plan_manager", None)
    if plans is not None:
        plans.apply_to(gate.state)
    else:
        gate.state.plan_executing = False
        gate.state.plan_scope = frozenset()
    if mode is ExecutionMode.AUTO:
        # AUTO has no prompt to approve a location with, so locations the user
        # granted in MANUAL for an earlier task do not carry over.
        boundary = getattr(gate, "boundary", None)
        if boundary is not None:
            boundary.clear_grants()


def _leave_plan(repl: Any) -> list[str]:
    """Plan-state consequences of leaving PLAN mode; returns lines to tell the user."""
    plans = getattr(repl, "_plan_manager", None)
    if plans is None:
        return []
    name = plans.active.name if plans.active else "the plan"
    if plans.suspend():
        return [
            f"[yellow]Plan {name} was executing and is now paused.[/yellow]",
            "[dim]It needs a fresh approval in PLAN mode to continue.[/dim]",
        ]
    if plans.waiting:
        return [
            f"[yellow]Plan {name} is still waiting for approval and will not run "
            "outside PLAN mode.[/yellow]"
        ]
    return []


def set_execution_mode(repl: VeluneREPL, mode: ExecutionMode, *, announce: bool = True) -> None:
    """Switch the session's execution mode and tell the user what changed."""
    previous = getattr(repl, "_execution_mode", ExecutionMode.MANUAL)
    plan_notes: list[str] = []
    if previous is ExecutionMode.PLAN and mode is not ExecutionMode.PLAN:
        plan_notes = _leave_plan(repl)
    repl._execution_mode = mode
    _sync_policy(repl, mode)
    try:
        repl._status_state.execution_label = mode_badge(repl)
    except Exception:
        pass
    if not announce:
        return
    if previous is mode:
        repl.console.print(f"[dim]Execution mode is already[/dim] [bold]{mode.label}[/bold].")
        return
    lines = [
        f"[bold]Execution mode changed:[/bold] {previous.label} → [bold]{mode.label}[/bold]",
        "",
    ]
    lines += [f"[dim]{line}[/dim]" for line in MODE_DESCRIPTIONS[mode]]
    lines += [""] + [f"[dim]{line}[/dim]" for line in MODE_LIMITS[mode]]
    if plan_notes:
        lines += [""] + plan_notes
    lines += ["", "[dim]Shift+Tab cycles MANUAL → PLAN → AUTO.[/dim]"]
    repl.console.print("\n".join(lines))


def _flash_notice(repl: Any, text: str) -> None:
    """Show *text* in the status bar for a moment, then redraw without it."""
    state = getattr(repl, "_status_state", None)
    if state is None:
        return
    state.mode_notice = text
    state.mode_notice_until = time.monotonic() + NOTICE_SECONDS
    try:
        import asyncio

        from prompt_toolkit.application.current import get_app_or_none

        def _redraw() -> None:
            app = get_app_or_none()
            if app is not None:
                app.invalidate()

        asyncio.get_running_loop().call_later(NOTICE_SECONDS + 0.1, _redraw)
    except Exception:
        pass


def cycle_execution_mode(repl: VeluneREPL) -> ExecutionMode:
    """Shift+Tab: MANUAL → PLAN → AUTO → MANUAL, confirmed briefly in the status bar."""
    current = getattr(repl, "_execution_mode", ExecutionMode.MANUAL)
    nxt = current.next()
    set_execution_mode(repl, nxt, announce=False)
    _flash_notice(repl, f"{nxt.label}: {MODE_HINTS[nxt]}")
    return nxt
