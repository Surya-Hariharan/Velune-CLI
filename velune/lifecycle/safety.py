"""Shared safety checks and manual-command text for lifecycle operations.

Windows: refuse to change the installation Velune is running from
------------------------------------------------------------------
In the P2.0 spike (uv 0.12.3, Windows 11), running ``uv tool install``,
``uv tool upgrade`` or ``uv tool uninstall`` against a tool that was running
failed with "Access is denied" / "being used by another process" *after* uv had
already started replacing files. The result was a half-replaced or destroyed
tool environment (``Lib/`` removed, or a new version active despite exit 1).

The process running a lifecycle command *is* Velune, so it holds the launcher
and the environment's ``python.exe`` open. Checking for *other* running
processes would therefore miss the one that always matters. The rule is:
on Windows, never mutate the installation that contains the running
interpreter; print the manual command instead.

No detached helper, no background updater, no self-replacement.

Other platforms: the spike did not test macOS or Linux, and POSIX file
semantics differ (an open file can be unlinked). No equivalent check is
justified by the evidence, so none is applied; this is recorded as an open
question, not a guarantee that in-place replacement is safe there.
"""

from __future__ import annotations

import ntpath
import os
import re
import sys
from pathlib import Path

from velune.lifecycle.errors import ExitCode, LifecycleError
from velune.lifecycle.ownership import DISTRIBUTION, OwnershipReport

_VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.!+_-]*")
_EXTRA_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def _norm(path: str | Path) -> str:
    """Comparable form of a Windows installation path (case, slashes and ``..`` folded).

    On Windows the path is resolved first. Anywhere else it is a Windows-style path being compared
    on a non-Windows host (the guard is asked about ``platform="win32"``), where the host's
    ``Path`` would treat ``C:/x`` as relative and ``normcase`` would not fold case, so it is
    normalised with ``ntpath`` instead.
    """
    if os.name != "nt":
        return ntpath.normcase(ntpath.normpath(str(path)))
    p = Path(path)
    try:
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved = p.absolute()
    return os.path.normcase(str(resolved))


def _requirement(extras: tuple[str, ...], version: str | None = None) -> str:
    """``velune-cli``, ``velune-cli[rag]`` or ``velune-cli[rag]==1.2.3``, quoted for any shell."""
    spec = DISTRIBUTION
    safe_extras = [e for e in extras if _EXTRA_RE.fullmatch(e)]
    if safe_extras:
        spec += "[" + ",".join(safe_extras) + "]"
    if version and _VERSION_RE.fullmatch(version):
        spec += f"=={version}"
    return f'"{spec}"' if any(c in spec for c in "[]=,") else spec


def manual_command(operation: str, report: OwnershipReport) -> str:
    """The exact uv command a user can run themselves for *operation*.

    Only commands exercised in the P2.0 spike are produced:

    * ``update`` on an unpinned install -> ``uv tool upgrade`` (kept extras).
    * ``update`` on a pinned install -> a forced reinstall. ``uv tool upgrade``
      answers "Nothing to upgrade" for an exact pin, and a reinstall without
      the extras *drops* them, so the extras are repeated explicitly.
    * ``repair`` -> forced reinstall of the current version with its extras.
    * ``uninstall`` -> ``uv tool uninstall``.
    """
    if operation == "uninstall":
        return f"uv tool uninstall {DISTRIBUTION}"
    if operation == "update":
        if report.specifier is None and not report.installed_from_local_file:
            return f"uv tool upgrade {DISTRIBUTION}"
        return f"uv tool install --force {_requirement(report.extras)}"
    if operation == "repair":
        return pinned_install_command(report, report.version)
    raise ValueError(f"unknown lifecycle operation: {operation!r}")


def pinned_install_command(report: OwnershipReport, version: str | None) -> str:
    """Forced reinstall at exactly *version*, keeping the installed extras.

    ``==version`` is dropped if it does not look like a version, so text from
    package metadata or an index can never reach a command line unvalidated.
    """
    return f"uv tool install --force {_requirement(report.extras, version)}"


def check_not_self_mutation(
    operation: str,
    report: OwnershipReport,
    *,
    platform: str | None = None,
    current_prefix: str | Path | None = None,
) -> None:
    """Raise REFUSED if *operation* would replace the running installation on Windows.

    Fails closed: on Windows, anything other than a clearly *different*
    environment is refused, including a prefix that cannot be resolved.
    """
    if (platform or sys.platform) != "win32":
        return
    try:
        target = _norm(report.prefix)
        running = _norm(current_prefix if current_prefix is not None else sys.prefix)
    except (OSError, ValueError):
        target = running = ""  # unresolvable: treat as the same installation
    if target and running and target != running:
        return
    command = manual_command(operation, report)
    raise LifecycleError(
        ExitCode.REFUSED,
        f"Velune cannot {operation} itself on Windows: the running Velune holds its own "
        "files open, and replacing them can leave a broken installation. Nothing was changed.",
        recovery=(
            "Close Velune, open a fresh terminal (not inside Velune), and run the command "
            "below. If the installation is already damaged, running it again after Velune "
            "has exited usually repairs it."
        ),
        manual_command=command,
        details={"reason": "windows_running_installation", "operation": operation},
    )
