"""``/manual`` ``/plan`` ``/auto`` — switch the execution mode."""

from __future__ import annotations

from typing import TYPE_CHECKING

from velune.cli.execution_modes import set_execution_mode
from velune.permissions import ExecutionMode

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL


async def cmd_set_mode(repl: VeluneREPL, mode: ExecutionMode, args: str = "") -> None:
    set_execution_mode(repl, mode)
    task = args.strip()
    if task and mode is ExecutionMode.PLAN:
        # "/plan add login rate limiting" = switch to PLAN and plan that task.
        await repl._handle_prompt(task)
    elif task:
        repl.console.print(
            f"[dim]Mode switched. Send your request as a normal message: {task!r}[/dim]"
        )
