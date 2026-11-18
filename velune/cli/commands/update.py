"""``velune update`` — check for, apply, or (on Windows) schedule a newer release.

Thin CLI surface. ``--check`` is :mod:`velune.lifecycle.check` (read-only, any
installation). Without it, :mod:`velune.lifecycle.apply` updates a confirmed
uv-managed tool and refuses everything else before changing anything.
"""

from __future__ import annotations

import json
import sys

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
        help="Only check whether a newer release exists. Read-only; changes nothing.",
    ),
    pre: bool = typer.Option(
        False, "--pre", help="Also consider pre-releases (for this check only)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Check for, or apply, a newer Velune release.

    With --check nothing is changed. Without it, a uv-managed installation is
    updated with 'uv tool upgrade'. On Windows (and for pip --user / venv installs
    there) the update is scheduled instead: it runs as soon as this command and
    every other Velune window have exited, and the next start reports the result.

    Exit codes: 0 up to date / updated, 10 update available (--check), 3 refused before any
    change (not a confirmed uv tool, pinned, Windows, unfinished journal, uv missing), 4 the
    package index could not be read, 8 another lifecycle command is running, 1 failed with
    the installation verified unchanged, 9 outcome uncertain or partial (journal kept: stop
    and read the recovery text), 2 bad usage, 130 cancelled.
    """
    if not check:
        if pre:
            _emit(
                LifecycleResult(
                    "update",
                    exit_code=ExitCode.USAGE,
                    message="--pre only works with --check; applying a pre-release is not supported.",
                    recovery="Use 'velune update --check --pre' to see whether one exists.",
                ),
                json_output,
            )
            return
        if sys.platform == "win32":
            # Windows cannot replace a running installation (safety.py, P2.0), and this
            # command *is* a running Velune. Hand the update to the post-exit helper, which
            # starts as soon as this process and every other Velune window have exited.
            from velune.lifecycle.autoupdate import run_scheduled_update

            _emit(run_scheduled_update(), json_output)
            return
        from velune.lifecycle.apply import run_update

        _emit(run_update(), json_output)
        return

    from velune.lifecycle.check import run_update_check

    _emit(run_update_check(include_prereleases=pre), json_output)
