"""``velune update --check`` — is a newer release available? (read-only)

Thin CLI surface over :mod:`velune.lifecycle.check`. Applying updates is not
implemented yet: without ``--check`` the command says so and exits with the
usage code instead of doing anything.
"""

from __future__ import annotations

import json

import typer

from velune.lifecycle.errors import ExitCode, LifecycleResult


def _emit(result: LifecycleResult, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    else:
        typer.echo(result.render_text(), err=not result.ok)
    raise typer.Exit(int(result.exit_code))


def update_cmd(
    check: bool = typer.Option(
        False,
        "--check",
        help="Check whether a newer release exists. Read-only; changes nothing.",
    ),
    pre: bool = typer.Option(
        False, "--pre", help="Also consider pre-releases (for this check only)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Check whether a newer Velune release is available.

    Exit codes: 0 up to date, 10 update available, 3 this installation is not a
    uv-managed tool (nothing checked), 4 the package index could not be read,
    1 other error, 2 bad usage.
    """
    if not check:
        _emit(
            LifecycleResult(
                "update",
                exit_code=ExitCode.USAGE,
                message="Applying updates is not available yet.",
                recovery="Use 'velune update --check' to see whether a newer release exists.",
            ),
            json_output,
        )
        return

    from velune.lifecycle.check import run_update_check

    _emit(run_update_check(include_prereleases=pre), json_output)
