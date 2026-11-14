"""The environment a plugin's child processes receive.

A plugin's hooks and MCP servers are separate programs written by someone else. Until now they
inherited Velune's whole environment, which is where provider API keys, cloud credentials and
tokens usually live, so any plugin could read them. (The MCP SDK ships a safe default for
exactly this reason; Velune passed the full environment over the top of it.)

A plugin's children now get an explicit, small environment:

* a short allowlist of variables a program needs just to run (where to find executables, the
  home and temp folders, locale, terminal, and on Windows the system folders), and
* whatever the plugin itself declares in its own ``env``, which is the only way to give a
  plugin a variable, and
* ``VELUNE_WORKSPACE`` for hooks.

Nothing is forwarded from the parent by pattern, so a secret with an unexpected name is kept
out too. There is deliberately no ``${VAR}`` forwarding: a plugin that needs a credential has
to be given it by value, in a config the user can read.

This policy is used for plugin-originated hooks and servers only. The user's own
``~/.velune/hooks.json`` and ``~/.mcp.json`` entries are written by the user and keep the full
environment, as before.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Mapping
from pathlib import Path

_log = logging.getLogger("velune.core.child_env")

#: Needed by almost any program to start and find its way around.
_POSIX_ALLOW = frozenset(
    {"HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER", "LANG", "LANGUAGE", "TZ", "TMPDIR"}
)

#: Windows has no HOME or LANG, but programs (and ``cmd.exe``, which runs hook commands there)
#: need the system folders. Names are compared case-insensitively on Windows.
_WINDOWS_ALLOW = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "HOMEDRIVE",
        "HOMEPATH",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERNAME",
        "USERPROFILE",
        "WINDIR",
    }
)

#: ``LC_ALL``, ``LC_CTYPE`` and the other locale categories.
_PREFIX_ALLOW = ("LC_",)


def plugin_child_env(
    declared: Mapping[str, str] | None = None,
    *,
    workspace: Path | str | None = None,
    base: Mapping[str, str] | None = None,
    windows: bool | None = None,
) -> dict[str, str]:
    """The environment for a plugin's child process. Never mutates its arguments.

    *declared* is the plugin's own ``env`` and always wins. *base* and *windows* exist so the
    policy can be tested for either platform; callers leave them out.
    """
    source = os.environ if base is None else base
    on_windows = (sys.platform == "win32") if windows is None else windows
    allowed = _WINDOWS_ALLOW if on_windows else _POSIX_ALLOW

    env: dict[str, str] = {}
    for key, value in source.items():
        name = key.upper() if on_windows else key
        if name not in allowed and not name.startswith(_PREFIX_ALLOW):
            continue
        if value.startswith("()"):  # an exported shell function, a known injection vector
            continue
        env[key] = value

    if workspace is not None:
        env["VELUNE_WORKSPACE"] = str(workspace)
    for key, value in (declared or {}).items():
        if _valid_entry(key, value):
            env[key] = value
        else:
            # Only the name is logged, never the value: it may well be a credential.
            _log.warning("Ignoring an invalid environment entry declared for a child: %r", key)
    return env


def _valid_entry(key: object, value: object) -> bool:
    """Whether (key, value) can be put in a process environment without making the spawn fail.

    A plugin declares its own ``env``; a name containing ``=``, an empty name, a NUL, or a
    non-text value makes the operating system refuse to start the process, which would break
    every call that fires the hook or server.
    """
    return (
        isinstance(key, str)
        and isinstance(value, str)
        and bool(key)
        and "=" not in key
        and "\x00" not in key
        and "\x00" not in value
    )
