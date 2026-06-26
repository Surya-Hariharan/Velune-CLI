"""Per-directory workspace trust store.

Opening Velune inside a directory historically caused project-controlled config
(``.mcp.json`` and ``velune.toml``) to be loaded and *acted on* automatically:
stdio MCP entries spawn arbitrary local processes, and ``[provider] base_url``
overrides redirect authenticated API traffic. A cloned/downloaded repository
could therefore achieve code execution or silently exfiltrate the user's API
keys the first time Velune was launched in it.

This module records an explicit, per-directory trust decision (Cursor /
Claude-Code style). Until a directory is trusted, callers must skip
project-level MCP servers and project ``base_url``/header overrides and fall
back to user-level (home-directory) configuration only.

The trust list lives in the non-synced application-data root (see
:mod:`velune.core.paths`) so it is shared across all workspaces but never
committed into any repository.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from velune.core.paths import app_data_root

logger = logging.getLogger("velune.core.trust")

_TRUST_FILE_NAME = "trusted_dirs.json"

# Set ``VELUNE_TRUST_ALL=1`` to bypass the trust gate entirely (CI / containers
# where the workspace is already known-good). This is an explicit, documented
# opt-out — never the default.
_TRUST_ALL_ENV = "VELUNE_TRUST_ALL"


def _trust_file() -> Path:
    return app_data_root() / _TRUST_FILE_NAME


def _canonical(path: Path | str) -> str:
    """Return a stable, comparable absolute key for *path*."""
    p = Path(path)
    try:
        resolved = p.resolve()
    except Exception:
        resolved = p.absolute()
    return str(resolved)


def trust_all_enabled() -> bool:
    """True when the global trust bypass env var is set to a truthy value."""
    return os.environ.get(_TRUST_ALL_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _load() -> dict[str, dict]:
    path = _trust_file()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            entries = data.get("trusted")
            if isinstance(entries, dict):
                return entries
    except Exception as exc:
        logger.debug("Could not read trust file %s: %s", path, exc)
    return {}


def _save(entries: dict[str, dict]) -> None:
    path = _trust_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "trusted": entries}
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not persist trust file %s: %s", path, exc)


def is_trusted(path: Path | str) -> bool:
    """Return True if *path* has been explicitly trusted (or trust is bypassed)."""
    if trust_all_enabled():
        return True
    return _canonical(path) in _load()


def trust(path: Path | str) -> None:
    """Record *path* as trusted."""
    key = _canonical(path)
    entries = _load()
    entries[key] = {"trusted_at": datetime.now(timezone.utc).isoformat()}
    _save(entries)
    logger.info("Workspace trusted: %s", key)


def forget(path: Path | str) -> bool:
    """Remove *path* from the trust list. Returns True if it was present."""
    key = _canonical(path)
    entries = _load()
    if key in entries:
        del entries[key]
        _save(entries)
        logger.info("Workspace trust revoked: %s", key)
        return True
    return False


def list_trusted() -> list[str]:
    """Return all currently trusted directory paths (sorted)."""
    return sorted(_load().keys())
