"""Persisted model preference — the user's chosen default model.

Stored as a tiny JSON file at ``~/.velune/active_model.json`` (mirroring the
atomic-write pattern used by :class:`~velune.cli.workspaces.WorkspaceRegistry`
and ``council_roles.json``). This lets ``/model use`` / ``/model connect``
survive across sessions: on startup the REPL resolves the stored
``{provider_id, model_id}`` against the model registry to restore the active
model without any network discovery.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger("velune.cli.model_prefs")

DEFAULT_PREFS_PATH = Path.home() / ".velune" / "active_model.json"


@dataclass(slots=True)
class ModelPref:
    """A persisted default-model reference."""

    provider_id: str
    model_id: str


def save_active_model(provider_id: str, model_id: str, path: Path | None = None) -> None:
    """Persist *provider_id*/*model_id* as the default model (atomic write)."""
    target = path or DEFAULT_PREFS_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {"provider_id": provider_id, "model_id": model_id}
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(target)
    except Exception as exc:
        _log.warning("Could not persist active model: %s", exc)


def load_active_model(path: Path | None = None) -> ModelPref | None:
    """Load the persisted default model reference, or None if unset/invalid."""
    target = path or DEFAULT_PREFS_PATH
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        provider_id = data.get("provider_id")
        model_id = data.get("model_id")
        if provider_id and model_id:
            return ModelPref(provider_id=provider_id, model_id=model_id)
    except Exception as exc:
        _log.warning("Could not load active model preference: %s", exc)
    return None


def clear_active_model(path: Path | None = None) -> None:
    """Remove the persisted default model reference, if any."""
    target = path or DEFAULT_PREFS_PATH
    try:
        target.unlink(missing_ok=True)
    except Exception as exc:
        _log.warning("Could not clear active model preference: %s", exc)
