"""``velune update --check``: is a newer Velune release available? Read-only.

What this does, in order, stopping at the first problem:

1. Work out how this copy was installed (P2.1 ownership detection). This only
   *labels* the result; it never blocks the check. Whether Velune may change an
   installation is a different question, answered by ``require_uv_tool`` in the
   mutating commands, and that gate is unchanged.
2. Make sure version comparison is possible (``packaging`` importable).
3. Read the installed version from package metadata.
4. Fetch the published file list from PyPI's Simple API (metadata only).
5. Select the latest applicable release and compare.

It never runs uv install/upgrade/uninstall, writes no journal or receipt,
touches no configuration or user data, and downloads no package files. The only
subprocess it may start is the read-only ``uv tool dir`` inside ownership
detection. The command shown to the user is text for them to run; Velune does
not run it, and it is only offered for a confirmed uv-managed tool. For every
other installation the user is pointed at whatever owns it.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import Any

from velune.lifecycle.errors import ExitCode, LifecycleError, LifecycleResult
from velune.lifecycle.index import INDEX_HOST, fetch_releases, index_override_warnings
from velune.lifecycle.ownership import (
    OwnershipKind,
    OwnershipReport,
    detect_ownership,
)
from velune.lifecycle.safety import manual_command, pinned_install_command
from velune.lifecycle.versions import (
    VersionTools,
    compare,
    load_version_tools,
    parse_installed,
    printable,
    select_latest,
)

COMMAND = "update"


_NOT_UV_GUIDANCE = {
    OwnershipKind.NOT_UV: (
        "This Velune was not installed as a uv tool. Update it with the tool that "
        "installed it (for example pip or pipx), or reinstall with the official "
        "installer (see the Install section of the README)."
    ),
    OwnershipKind.EDITABLE: (
        "This is a development (editable) install. Update your development checkout "
        "with git instead."
    ),
    OwnershipKind.AMBIGUOUS: (
        "Velune could not confirm how it was installed, so no update command is "
        "suggested. Run 'velune doctor' for details, or reinstall with the official "
        "installer (see the Install section of the README)."
    ),
    OwnershipKind.INDETERMINATE: (
        "Velune could not determine how it was installed, so no update command is "
        "suggested. Run 'velune doctor' for details, or reinstall with the official "
        "installer (see the Install section of the README)."
    ),
}


def _next_action(report: OwnershipReport, latest: Any, platform: str) -> tuple[str | None, str]:
    """``(command, guidance)`` for moving to *latest*. Never executed by Velune.

    The command is ``None`` unless the installation is a confirmed uv tool.
    """
    if report.kind is not OwnershipKind.UV_TOOL:
        return None, _NOT_UV_GUIDANCE[report.kind]
    if latest.is_prerelease:
        # `uv tool upgrade` is not shown to honour pre-release opt-in; an exact
        # install is the form the spike verified.
        command = pinned_install_command(report, str(latest))
    else:
        command = manual_command("update", report)
    if platform == "win32":
        guidance = (
            "Updates are manual-only on Windows: close Velune, open a fresh terminal, "
            "and run the command below."
        )
    else:
        guidance = "To update, run the command below in your terminal."
    return command, guidance


def _common_warnings(report: OwnershipReport, environ: Mapping[str, str]) -> list[str]:
    notes = index_override_warnings(environ)
    if report.kind in (OwnershipKind.AMBIGUOUS, OwnershipKind.INDETERMINATE):
        notes.append("Ownership could not be confirmed; this installation will not be changed.")
    return notes


def _warnings(report: OwnershipReport, latest: Any, environ: Mapping[str, str]) -> list[str]:
    notes = _common_warnings(report, environ)
    if report.kind is not OwnershipKind.UV_TOOL:
        return notes
    if report.specifier:
        notes.append(
            f"This installation is pinned ({printable(report.specifier)}), so "
            "'uv tool upgrade' would not move it; the command below replaces the pin."
        )
    if latest.is_prerelease:
        notes.append(
            "The target is a pre-release and the command pins it exactly; later "
            "'uv tool upgrade' runs will not move past it."
        )
    if report.installed_from_local_file:
        notes.append(
            "This copy was installed from a local file; the command below switches it "
            "to the package index."
        )
    return notes


def run_update_check(
    *,
    include_prereleases: bool = False,
    detect: Callable[[], OwnershipReport] = detect_ownership,
    load_tools: Callable[[], VersionTools] = load_version_tools,
    fetch: Callable[[], Mapping[str, Any]] = fetch_releases,
    python: tuple[int, int, int] | None = None,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
) -> LifecycleResult:
    """Run the check and return a result; never raises."""
    installation: dict[str, Any] = {}
    try:
        report = detect()
        installation = report.installation

        tools = load_tools()
        installed = parse_installed(tools, report.version)
        releases = fetch()

        py = python or (sys.version_info.major, sys.version_info.minor, sys.version_info.micro)
        latest = select_latest(tools, releases, python=py, include_prereleases=include_prereleases)
        if latest is None:
            raise LifecycleError(
                ExitCode.NETWORK,
                f"{INDEX_HOST} lists no installable Velune release for this Python. "
                "Nothing was changed.",
                recovery="Try again later, or see the README for installation options.",
                details={"reason": "no_applicable_release"},
            )
        env = os.environ if environ is None else environ
        outcome = compare(installed, latest)
        data = {
            "installed_version": str(installed),
            "latest_version": str(latest),
            "update_available": outcome == "update",
            "include_prereleases": include_prereleases,
            "index": INDEX_HOST,
        }
        if outcome == "update":
            command, guidance = _next_action(report, latest, platform or sys.platform)
            return LifecycleResult(
                COMMAND,
                exit_code=ExitCode.UPDATE_AVAILABLE,
                message=f"Update available: Velune {installed} -> {latest} (from {INDEX_HOST}).",
                recovery=guidance,
                manual_command=command,
                installation=installation,
                result=data,
                warnings=tuple(_warnings(report, latest, env)),
            )
        notes = tuple(_common_warnings(report, env))
        if outcome == "same":
            message = f"Velune {installed} is up to date (latest on {INDEX_HOST}: {latest})."
        else:
            message = (
                f"Velune {installed} is newer than the latest release on {INDEX_HOST} ({latest})."
            )
        return LifecycleResult(
            COMMAND,
            message=message,
            installation=installation,
            result=data,
            warnings=notes,
        )
    except BaseException as exc:  # mapped to a result; unexpected text is never leaked
        if isinstance(exc, SystemExit):
            raise
        return LifecycleResult.from_exception(COMMAND, exc, installation=installation)
