"""Non-blocking accept/reject prompt for file-change approval.

The diff previews used to call ``rich.prompt.Prompt.ask`` from async code. That is a blocking
``input()``: it froze the event loop (and with it the full-screen prompt_toolkit app that owns
stdin), left two readers on one terminal, had no timeout and no Ctrl+C path, and its label
``[a]ccept / [r]eject`` was eaten by Rich markup (``ccept / eject``).

This routes through the shared interactive widgets instead, which draw inside the running REPL
when a host is installed. Anything other than an explicit choice (no TTY, cancel, timeout, error)
is a **rejection**: a file write must never happen by accident.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

# Seconds to wait for a decision before rejecting. Generous: a human is reading a diff.
APPROVAL_TIMEOUT_S = 600.0


async def ask_choice(
    title: str,
    choices: Sequence[tuple[str, str]],
    *,
    default: str | None = None,
    reject: str,
    timeout: float = APPROVAL_TIMEOUT_S,
) -> str:
    """Return the chosen id from *choices* ``[(id, label), ...]``.

    Returns *reject* when nobody can answer (no terminal), the user cancels or goes back, the
    wait times out, or the widget fails. *default* is only the highlighted row, never an
    implicit answer.
    """
    from velune.cli.interactive import CANCEL, Option, is_interactive_tty, single_select

    if not is_interactive_tty():
        return reject

    options = [Option(id=cid, label=label) for cid, label in choices]
    initial = next((i for i, (cid, _) in enumerate(choices) if cid == default), 0)
    try:
        answer = await asyncio.wait_for(
            single_select(title, options, palette=True, initial_index=initial),
            timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # includes TimeoutError: fail closed
        return reject
    if answer is CANCEL or not isinstance(answer, str):
        return reject
    return answer if any(answer == cid for cid, _ in choices) else reject
