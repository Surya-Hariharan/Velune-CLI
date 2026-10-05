"""Execution mode (MANUAL / PLAN / AUTO): who may change what, from the user's side.

Separate from the fast/normal/max *session* modes (velune/cli/modes.py), which
pick model tier and depth. This module owns the startup value, switching
(``/manual`` ``/plan`` ``/auto``, Shift+Tab), and the message shown on each
switch. Enforcement lives in velune.permissions and the tool layer.
"""

from __future__ import annotations

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
        "Velune can modify files and run allowed commands without asking",
        "at every step.",
    ],
}
SAFETY_NOTE = [
    "Workspace boundary remains active.",
    "High-risk operations still require confirmation.",
]

BADGES: dict[ExecutionMode, str] = {
    ExecutionMode.MANUAL: "MANUAL",
    ExecutionMode.PLAN: "⏸ PLAN",
    ExecutionMode.AUTO: "⏵⏵ AUTO",
}


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


def set_execution_mode(repl: VeluneREPL, mode: ExecutionMode, *, announce: bool = True) -> None:
    """Switch the session's execution mode and tell the user what changed."""
    previous = getattr(repl, "_execution_mode", ExecutionMode.MANUAL)
    repl._execution_mode = mode
    gate = getattr(repl, "_permission_gate", None)
    if gate is not None:
        gate.state.mode = mode
    try:
        repl._status_state.execution_label = BADGES[mode]
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
    if mode is not ExecutionMode.PLAN:
        lines += [""] + [f"[dim]{line}[/dim]" for line in SAFETY_NOTE]
    lines += ["", "[dim]Shift+Tab cycles MANUAL → PLAN → AUTO.[/dim]"]
    repl.console.print("\n".join(lines))


def cycle_execution_mode(repl: VeluneREPL) -> ExecutionMode:
    """Shift+Tab: MANUAL → PLAN → AUTO → MANUAL (quiet: the status bar shows it)."""
    current = getattr(repl, "_execution_mode", ExecutionMode.MANUAL)
    nxt = current.next()
    set_execution_mode(repl, nxt, announce=False)
    return nxt
