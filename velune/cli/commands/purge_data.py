"""``velune purge-data`` — delete selected categories of Velune's own user data.

Deliberately NOT part of ``velune uninstall`` (which never touches data): a
separate command with a preview by default, explicit categories, and a typed
confirmation phrase. See ``docs/data-inventory.md``.
"""

from __future__ import annotations

import json
import sys

import typer

from velune.lifecycle.errors import LifecycleResult


def _emit(result: LifecycleResult, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    else:
        typer.echo(result.render_text(), err=not result.ok)
    raise typer.Exit(int(result.exit_code))


def _ask(preview: str) -> str:
    from velune.lifecycle.purge import CONFIRM_PHRASE

    typer.echo(preview)
    typer.echo("\nThis permanently deletes the items above and cannot be undone.")
    return typer.prompt(f'Type "{CONFIRM_PHRASE}" to continue', default="", show_default=False)


def purge_data_cmd(
    category: list[str] = typer.Option(
        [],
        "--category",
        "-c",
        help="Category to delete (repeatable): cache, logs, state, config, sessions, skills, "
        "credentials, memory. Nothing is selected by default.",
    ),
    execute: bool = typer.Option(
        False, "--execute", help="Actually delete. Without it this is a read-only preview."
    ),
    confirm_phrase: str = typer.Option(
        "",
        "--confirm-phrase",
        help="Non-interactive confirmation: the exact phrase shown by the interactive prompt.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Preview or delete selected Velune user data. Never touches the program or projects.

    With no options: a read-only inventory. With --category: a preview of exactly what
    would be deleted. Deleting needs --execute plus a typed confirmation phrase
    (interactive prompt, or --confirm-phrase when there is no terminal). Credentials,
    memory, skills and sessions are only deleted when named explicitly.

    --confirm-phrase is scriptable and the phrase is public, so it lets any script or AI agent
    with shell access delete the selected data. It is not the same as an interactive
    confirmation; do not use it in unattended automation (see docs/lifecycle.md).

    Exit codes: 0 done / preview, 2 unknown category, 3 refused (problems found, no
    confirmation), 130 declined, 1 stopped before deleting anything, 9 stopped after deleting
    something (partial result).
    """
    from velune.lifecycle.purge import run_purge

    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not json_output
    _emit(
        run_purge(
            tuple(category),
            execute=execute,
            confirm=_ask if interactive else None,
            confirm_phrase=confirm_phrase or None,
        ),
        json_output,
    )
