"""Single writer of ``providers.default_provider`` in ``velune.toml``.

Before this module, two independent call sites wrote this same key:
``cli/handlers/model.py``'s ``activate_model`` (triggered by ``/model use``)
and ``cli/commands/providers.py``'s ``_maybe_set_first_default`` /
``provider_default`` (triggered by ``velune provider add`` / ``velune
provider default``). Neither was aware the other existed, so the two could
race or disagree about which trigger "wins". This module is the one place
that write happens now; both call sites route through it.
"""

from __future__ import annotations

import logging
from pathlib import Path

_log = logging.getLogger("velune.providers.default_provider")


def find_config_path(start: Path | None = None) -> Path | None:
    """Walk up from *start* (default: cwd) looking for ``velune.toml`` (max 8 levels)."""
    current = (start or Path.cwd()).resolve()
    for _ in range(8):
        candidate = current / "velune.toml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def get_default_provider(start: Path | None = None) -> str | None:
    """Read ``providers.default_provider`` from ``velune.toml``, or None if unset."""
    path = find_config_path(start)
    if not path:
        return None
    try:
        import toml

        return toml.load(path).get("providers", {}).get("default_provider")
    except Exception:
        return None


def set_default_provider(
    provider_id: str,
    *,
    config_path: Path | None = None,
    start: Path | None = None,
) -> Path | None:
    """Write ``providers.default_provider`` into ``velune.toml``.

    If *config_path* is given, it is used directly (this is how a REPL
    session with a known workspace/config path resolves it). Otherwise the
    path is found by walking up from *start* (default: cwd) the same way
    ``velune provider ...`` resolves it, falling back to creating
    ``velune.toml`` under *start*/cwd if none exists yet.

    Returns the path written to, or None on failure (never raises — this is
    a best-effort convenience write, not a correctness-critical one).
    """
    try:
        import toml

        path = config_path or find_config_path(start) or ((start or Path.cwd()) / "velune.toml")
        data = toml.load(path) if path.exists() else {}
        data.setdefault("providers", {})["default_provider"] = provider_id
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            toml.dump(data, fh)
        return path
    except Exception as exc:
        _log.debug("Could not persist default provider: %s", exc)
        return None


def set_first_default(
    provider_id: str,
    *,
    config_path: Path | None = None,
    start: Path | None = None,
) -> bool:
    """Set *provider_id* as default iff no default is configured yet.

    A brand-new user's first provider should "just work" without a separate
    ``velune provider default`` step, mirroring how git/gh adopt the first
    configured remote/account. Returns ``True`` if this call set the default.
    Never overrides an existing choice.
    """
    if get_default_provider(start=start):
        return False
    return set_default_provider(provider_id, config_path=config_path, start=start) is not None
