"""``velune uninstall`` — remove the Velune program (a uv tool); keep all user data.

Thin CLI surface over :mod:`velune.lifecycle.uninstall`. No purge option exists.
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


def _ask(text: str) -> bool:
    typer.echo(text)
    return typer.confirm("Uninstall Velune?", default=False)


def uninstall_cmd(
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Confirm in advance. Skips only the question; every safety check still applies.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Uninstall the Velune program from a uv-managed install. Keeps your data.

    Runs 'uv tool uninstall velune-cli'. Removes only the program; configuration,
    credentials, memory, skills, projects, the uv cache and other uv tools stay.
    Refused for pip/editable/ambiguous installs and on Windows (manual there).
    Without a terminal to ask in, it refuses unless --yes is given.

    Exit codes: 0 uninstalled, 3 refused before any change (including uv missing), 8 another
    lifecycle command is running, 1 failed with the installation verified intact, 9 outcome
    uncertain (journal kept: stop and read the recovery text), 130 declined or cancelled.

    --yes is scriptable: it lets any script or agent with shell access remove the program.
    It is not the same as an interactive confirmation; see docs/lifecycle.md.
    """
    from velune.lifecycle.uninstall import run_uninstall

    interactive = sys.stdin.isatty() and sys.stdout.isatty() and not json_output
    _emit(run_uninstall(confirm=_ask if interactive else None, assume_yes=yes), json_output)
