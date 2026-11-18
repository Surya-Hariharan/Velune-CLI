"""Where pip put the ``velune`` launcher, and whether that directory is on PATH.

``pip install velune-cli`` always generates the console launchers; whether a fresh terminal can
*find* them depends on the Scripts directory being on PATH. For ``pip install --user`` on Windows
that directory is ``%APPDATA%\\Python\\PythonXY\\Scripts``, which Python's installer does not add
to PATH. Directories come from ``sysconfig`` (never a hardcoded Python version) and this module
only *reports*: it never changes PATH or the environment. Standard library only, so it is cheap
to import and keeps working in a damaged environment.
"""

from __future__ import annotations

import os
import sys
import sysconfig
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LAUNCHER_STEM = "velune"


def candidate_scripts_dirs() -> list[Path]:
    """Scripts directories pip may have used for this interpreter, most specific first."""
    dirs: list[Path] = []

    def add(raw: str | None) -> None:
        if raw:
            path = Path(raw)
            if path not in dirs:
                dirs.append(path)

    try:
        user_scheme = sysconfig.get_preferred_scheme("user")
        add(sysconfig.get_path("scripts", scheme=user_scheme))
    except (KeyError, ValueError):
        pass
    try:
        add(sysconfig.get_path("scripts"))
    except (KeyError, ValueError):
        pass
    return dirs


def launcher_name(platform: str | None = None) -> str:
    return f"{LAUNCHER_STEM}.exe" if (platform or sys.platform) == "win32" else LAUNCHER_STEM


def find_launcher_dir(dirs: list[Path], platform: str | None = None) -> Path | None:
    """The first directory in *dirs* that actually contains the generated launcher."""
    name = launcher_name(platform)
    for directory in dirs:
        try:
            if (directory / name).is_file():
                return directory
        except OSError:
            continue
    return None


def _normalise(entry: str, platform: str) -> str:
    entry = os.path.expandvars(entry.strip().strip('"'))
    if platform == "win32":
        return entry.replace("/", "\\").rstrip("\\").lower()
    return entry.rstrip("/")


def dir_on_path(
    directory: Path | str, environ: Mapping[str, str] | None = None, platform: str | None = None
) -> bool:
    """Is *directory* an entry of PATH? Case-insensitive on Windows, no filesystem access."""
    plat = platform or sys.platform
    env = os.environ if environ is None else environ
    sep = ";" if plat == "win32" else ":"
    target = _normalise(str(directory), plat)
    return any(_normalise(p, plat) == target for p in env.get("PATH", "").split(sep) if p.strip())


def path_fix_advice(directory: Path | str, platform: str | None = None) -> str:
    """Copy-paste remediation. Velune prints this; it never runs it."""
    plat = platform or sys.platform
    if plat == "win32":
        quoted = str(directory).replace("'", "''")
        return (
            "Easiest: run 'python -m velune doctor --fix', then open a NEW terminal. Or, "
            "to add it for your user account, run this in PowerShell, then open a NEW "
            "terminal: [Environment]::SetEnvironmentVariable('Path', "
            "[Environment]::GetEnvironmentVariable('Path','User') + ';" + quoted + "', 'User')"
            " -- or run 'python -m velune' without changing PATH."
        )
    return (
        f'To add it, put this in your shell profile: export PATH="{directory}:$PATH" -- '
        "or run 'python -m velune' without changing PATH."
    )


def add_to_user_path(directory: Path | str) -> tuple[bool, str]:
    """Append *directory* to the Windows *user* PATH. Only called on explicit request (``doctor --fix``).

    Returns ``(changed, message)``. Never touches the machine-wide PATH. Elsewhere it changes nothing
    and returns the shell-profile advice, since there is no single file to edit safely.
    """
    if sys.platform != "win32":
        return False, path_fix_advice(directory)
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE
        ) as key:
            try:
                current, kind = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current, kind = "", winreg.REG_EXPAND_SZ
            if dir_on_path(directory, {"PATH": current}, "win32"):
                return False, f"{directory} is already on your user PATH."
            new = f"{current.rstrip(';')};{directory}" if current else str(directory)
            winreg.SetValueEx(key, "Path", 0, kind, new)
    except OSError as exc:
        return False, f"Could not update PATH ({exc}). {path_fix_advice(directory)}"
    try:  # tell running programs (Explorer) to reload the environment; best effort
        import ctypes

        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, None
        )
    except Exception:
        pass
    return True, f"Added {directory} to your user PATH. Open a NEW terminal for it to take effect."


@dataclass(frozen=True)
class ScriptsPathReport:
    scripts_dir: Path | None  # where the launcher was found (else the best candidate)
    launcher_present: bool
    on_path: bool
    advice: str | None  # set only when launcher_present and not on_path


def diagnose(
    *,
    dirs: list[Path] | None = None,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
) -> ScriptsPathReport:
    """Report whether the launcher exists and whether its directory is discoverable."""
    plat = platform or sys.platform
    candidates = candidate_scripts_dirs() if dirs is None else dirs
    found = find_launcher_dir(candidates, plat)
    if found is None:
        return ScriptsPathReport(candidates[0] if candidates else None, False, False, None)
    on_path = dir_on_path(found, environ, plat)
    return ScriptsPathReport(
        found, True, on_path, None if on_path else path_fix_advice(found, plat)
    )
