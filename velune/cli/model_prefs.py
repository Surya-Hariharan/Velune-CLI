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
        data = {}
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                pass
        data["provider_id"] = provider_id
        data["model_id"] = model_id
        
        # Add to recents automatically
        recents = data.setdefault("recents", [])
        if model_id in recents:
            recents.remove(model_id)
        recents.insert(0, model_id)
        data["recents"] = recents[:10]  # Keep last 10
        
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
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


def load_favorites(path: Path | None = None) -> list[str]:
    """Load favorited model IDs."""
    target = path or DEFAULT_PREFS_PATH
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data.get("favorites", [])
    except Exception:
        return []


def toggle_favorite(model_id: str, path: Path | None = None) -> bool:
    """Toggle a model ID in the favorites list. Returns the new favorite state."""
    target = path or DEFAULT_PREFS_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                pass
        favorites = data.setdefault("favorites", [])
        if model_id in favorites:
            favorites.remove(model_id)
            is_fav = False
        else:
            favorites.append(model_id)
            is_fav = True
        
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(target)
        return is_fav
    except Exception as exc:
        _log.warning("Could not toggle favorite: %s", exc)
        return False


def load_recents(path: Path | None = None) -> list[str]:
    """Load recently used model IDs."""
    target = path or DEFAULT_PREFS_PATH
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return data.get("recents", [])
    except Exception:
        return []


def add_recent(model_id: str, path: Path | None = None) -> None:
    """Add a model ID to the recently used list."""
    target = path or DEFAULT_PREFS_PATH
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
            except Exception:
                pass
        recents = data.setdefault("recents", [])
        if model_id in recents:
            recents.remove(model_id)
        recents.insert(0, model_id)
        data["recents"] = recents[:10]
        
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(target)
    except Exception as exc:
        _log.warning("Could not add recent model: %s", exc)

