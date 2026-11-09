"""``velune repair`` — reinstall the current version of a uv-managed Velune.

Thin CLI surface over :mod:`velune.lifecycle.repair`. Never upgrades, downgrades
or switches source; refuses everything that is not a confirmed, reproducible uv
tool installation before changing anything.
"""

from __future__ import annotations

import json

import typer

from velune.lifecycle.errors import LifecycleResult


def _emit(result: LifecycleResult, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    else:
        typer.echo(result.render_text(), err=not result.ok)
    raise typer.Exit(int(result.exit_code))


def repair_cmd(
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON."),
) -> None:
    """Reinstall the installed Velune version of a uv-managed copy.

    Runs 'uv tool install --force "velune-cli[extras]==<installed version>"'. The
    version and extras are kept; it is never an update. Refused for pip/editable/
    ambiguous installs, local-file or URL installs, version ranges, and on Windows
    (manual there). Verifies files afterwards but does not run the repaired CLI.

    Exit codes: 0 repaired and verified, 3 refused before any change (including uv missing),
    8 another lifecycle command is running, 1 failed with the installation verified intact,
    9 outcome uncertain (journal kept: stop and read the recovery text), 130 cancelled.
    """
    from velune.lifecycle.repair import run_repair

    _emit(run_repair(), json_output)
