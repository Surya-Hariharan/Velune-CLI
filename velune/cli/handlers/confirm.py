"""Shared confirmation prompt for destructive slash-command actions.

Used both by the dispatch-level ``permissions=("confirm",)`` gate
(``VeluneREPL._handle_slash_command``) and by individual handlers whose
destructive behavior lives behind a subcommand rather than the whole command
(e.g. ``/memory clear`` vs. the harmless ``/memory``/``/memory stats``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from velune.cli.interactive import confirm, is_interactive_tty

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL


async def confirm_destructive(repl: VeluneREPL, prompt: str, *, default: bool = False) -> bool:
    """Ask for confirmation before a destructive action; honors --yes / auto-accept.

    Async, and routed through the shared ``ConfirmWidget``. It previously called
    ``rich.prompt.Confirm.ask()`` — a *blocking, synchronous* stdin read — from
    inside the running fullscreen prompt_toolkit application, which owns stdin.
    Two readers on one terminal is not a thing you can do.
    """
    try:
        if bool(repl.container.get("runtime.auto_accept")):
            return True
    except Exception:
        pass

    # No TTY (piped input, CI): there is nobody to ask. Refuse rather than
    # silently proceeding with something destructive.
    if not is_interactive_tty():
        return False

    try:
        answer = await confirm(prompt.strip(), default=default)
    except Exception:
        return False

    # confirm() may return the BACK/CANCEL sentinel; only an explicit True is yes.
    return answer is True
