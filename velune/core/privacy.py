"""Local-path and secret scrubbing for diagnostics that users paste into bug reports.

``velune doctor --json`` is built to be copied into an issue tracker. Raw
output contains the interpreter path, install location and data directories,
all of which sit under the user's home directory and therefore carry their OS
username (``C:\\Users\\alice\\...``, ``/home/alice/...``).

:func:`sanitize_diagnostics` rewrites those home-directory prefixes to ``~``
(the rest of the path is kept, so ``~/AppData/Local/Velune`` is still useful)
and passes every string through :func:`velune.core.redaction.redact_secrets`.
Only usernames that appear *as part of a path* are normalised; a bare word is
never replaced, because a short username such as ``user`` would otherwise
garble ordinary sentences.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from velune.core.redaction import redact_secrets

HOME_PLACEHOLDER = "~"

# A path component that is a user name: anything up to the next separator,
# whitespace or quote. Names containing spaces are handled by the literal
# home-directory match below, which knows the real value.
_COMPONENT = r"[^\\/\s\"'<>|:*?]+"

_GENERIC_HOME_PATTERNS: tuple[re.Pattern[str], ...] = (
    # C:\Users\alice, C:/Users/alice, \\?\C:\Users\alice
    re.compile(r"(?:\\\\\?\\)?[A-Za-z]:[\\/]+Users[\\/]+" + _COMPONENT, re.IGNORECASE),
    # MSYS / Git Bash style: /c/Users/alice
    re.compile(r"(?<![\w.~-])/[A-Za-z]/Users/" + _COMPONENT, re.IGNORECASE),
    # macOS and Linux: /Users/alice, /home/alice
    re.compile(r"(?<![\w.~-])/(?:Users|home)/" + _COMPONENT),
)


def _literal_home_pattern(home: str) -> re.Pattern[str] | None:
    """Match the machine's real home directory, in either slash style."""
    stripped = home.strip().rstrip("\\/")
    if len(stripped) < 3:  # "", "/", "C:" — never worth matching
        return None
    parts = [re.escape(p) for p in re.split(r"[\\/]+", stripped) if p]
    if not parts:
        return None
    # ``C:\Users\alice`` and ``C:/Users/alice`` must both match.
    body = r"[\\/]+".join(parts)
    # A POSIX home starts with "/"; a Windows one may carry the extended-length
    # ``\\?\`` prefix, which must go with it or ``\\?\~`` is left behind.
    prefix = r"/" if stripped.startswith("/") else r"(?:\\\\\?\\)?"
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile(prefix + body + r"(?=[\\/\s\"'<>|:*?]|$)", flags)


def redact_local_paths(text: str, home: str | None = None) -> str:
    """Replace home-directory prefixes in *text* with ``~``.

    *home* defaults to the current user's home directory; it is a parameter so
    tests (and callers formatting another machine's report) can supply one.
    """
    if not text:
        return text
    if home is None:
        try:
            home = str(Path.home())
        except (RuntimeError, KeyError, OSError):
            home = ""
    literal = _literal_home_pattern(home)
    if literal is not None:
        text = literal.sub(HOME_PLACEHOLDER, text)
    for pattern in _GENERIC_HOME_PATTERNS:
        text = pattern.sub(HOME_PLACEHOLDER, text)
    return text


def sanitize_diagnostics(value: Any, home: str | None = None) -> Any:
    """Return a copy of *value* safe to publish.

    Walks dicts, lists and tuples; strings get home paths normalised and
    credential shapes redacted. Keys, numbers, booleans and ``None`` are left
    untouched, so the result still serialises to the same JSON structure.
    """
    if isinstance(value, str):
        return redact_secrets(redact_local_paths(value, home))
    if isinstance(value, dict):
        return {k: sanitize_diagnostics(v, home) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_diagnostics(v, home) for v in value]
    if isinstance(value, tuple):
        return tuple(sanitize_diagnostics(v, home) for v in value)
    return value
