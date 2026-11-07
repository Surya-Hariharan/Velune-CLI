"""Version comparison and release selection for the update check.

Policy (Phase 2 design §4, stated here so it is testable)
---------------------------------------------------------
* Versions are PEP 440 and compared with ``packaging.version.Version``. Never as
  strings: ``"0.9.10" < "0.9.9"`` is true lexicographically.
* The **latest applicable release** is the highest version that
    - parses as a valid version (invalid index entries are ignored, never guessed),
    - has at least one file that is not yanked,
    - is not a pre-release or dev release unless the caller asked for them
      (``--pre``, for that run only),
    - has a file whose ``requires_python`` accepts the running interpreter.
  Staying on a pre-release never grants permission to move to the next one.
* The installed version must itself be a valid version; otherwise the check
  stops. It is not treated as 0 or as "unknown but fine".
* An installed version newer than every published release (a local build, a
  yanked release) is reported as such, not as "update available".

``packaging`` is a core Velune dependency, but it is also exactly what a damaged
environment may be missing. There is deliberately **no fallback comparator**: a
home-grown one would silently disagree with PEP 440 on exactly the odd versions
that matter. If ``packaging`` cannot be imported the check stops with a clear
message instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from velune.lifecycle.errors import ExitCode, LifecycleError


@dataclass(frozen=True)
class VersionTools:
    """The ``packaging`` classes the check needs, imported lazily."""

    Version: Any
    InvalidVersion: Any
    SpecifierSet: Any
    InvalidSpecifier: Any


def load_version_tools() -> VersionTools:
    """Import ``packaging``; raise an ERROR result-able exception if it is unusable."""
    try:
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
        from packaging.version import InvalidVersion, Version
    except ImportError as exc:
        raise LifecycleError(
            ExitCode.ERROR,
            "Velune cannot compare versions because the 'packaging' library is missing or "
            "damaged in this Python environment. Nothing was changed.",
            recovery=(
                "Reinstall Velune with the official installer (see the Install section of "
                "the README), then run 'velune doctor'."
            ),
            details={"reason": "version_library_unavailable", "error_type": type(exc).__name__},
        ) from exc
    return VersionTools(Version, InvalidVersion, SpecifierSet, InvalidSpecifier)


def printable(text: str, limit: int = 40) -> str:
    """Make untrusted version text safe to show: allowed characters only, bounded."""
    return re.sub(r"[^0-9A-Za-z.!+_-]", "?", text)[:limit]


def parse_installed(tools: VersionTools, text: str | None) -> Any:
    if not text:
        raise LifecycleError(
            ExitCode.ERROR,
            "Velune could not read its own installed version, so it cannot check for updates.",
            recovery="Run 'velune doctor' to inspect the installation.",
            details={"reason": "installed_version_missing"},
        )
    try:
        return tools.Version(text)
    except tools.InvalidVersion as exc:
        raise LifecycleError(
            ExitCode.ERROR,
            f"The installed version '{printable(text)}' is not a valid version number, so "
            "it cannot be compared with the latest release.",
            recovery="Run 'velune doctor' to inspect the installation.",
            details={"reason": "installed_version_invalid"},
        ) from exc


def _accepts_python(tools: VersionTools, files: list[dict[str, Any]], python: str) -> bool:
    for entry in files:
        requires = entry.get("requires_python")
        if not requires:
            return True
        if not isinstance(requires, str):
            continue
        try:
            if tools.SpecifierSet(requires).contains(python, prereleases=True):
                return True
        except tools.InvalidSpecifier:
            continue  # cannot confirm compatibility from this file
    return False


def select_latest(
    tools: VersionTools,
    releases: Mapping[str, Any],
    *,
    python: tuple[int, int, int],
    include_prereleases: bool,
) -> Any | None:
    """Return the highest applicable ``Version`` in *releases*, or ``None``."""
    python_text = ".".join(str(part) for part in python)
    best = None
    for text, files in releases.items():
        try:
            version = tools.Version(text)
        except tools.InvalidVersion:
            continue
        if version.is_prerelease and not include_prereleases:
            continue
        if version.local is not None:
            continue  # local versions are not publishable on PyPI; never a target
        if not isinstance(files, list):
            continue
        usable = [f for f in files if isinstance(f, dict) and not f.get("yanked")]
        if not usable or not _accepts_python(tools, usable, python_text):
            continue
        if best is None or version > best:
            best = version
    return best


def compare(installed: Any, latest: Any) -> str:
    """``"update"`` if *latest* is newer, ``"same"`` if equal, ``"ahead"`` if older."""
    if latest > installed:
        return "update"
    if latest == installed:
        return "same"
    return "ahead"
