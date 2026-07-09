"""Shared confirmation prompt for destructive slash-command actions.

Used both by the dispatch-level ``permissions=("confirm",)`` gate
(``VeluneREPL._handle_slash_command``) and by individual handlers whose
destructive behavior lives behind a subcommand rather than the whole command
(e.g. ``/memory clear`` vs. the harmless ``/memory``/``/memory stats``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL


def confirm_destructive(repl: VeluneREPL, prompt: str, *, default: bool = False) -> bool:
    """Ask for confirmation before a destructive action; honors --yes / auto-accept."""
    from rich.prompt import Confirm

    try:
        if bool(repl.container.get("runtime.auto_accept")):
            return True
    except Exception:
        pass
    try:
        return Confirm.ask(prompt, default=default)
    except Exception:
        return False
