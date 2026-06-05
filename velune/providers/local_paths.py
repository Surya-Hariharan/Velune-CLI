"""Persistent JSON cache that maps model_id strings to resolved GGUF file paths."""

from __future__ import annotations

import json
from pathlib import Path


def _cache_path() -> Path:
    project = Path.cwd() / ".velune"
    if project.exists():
        return project / "model_paths.json"
    home = Path.home() / ".velune"
    home.mkdir(parents=True, exist_ok=True)
    return home / "model_paths.json"


def _load() -> dict[str, str]:
    path = _cache_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save(data: dict[str, str]) -> None:
    try:
        _cache_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def save_model_path(model_id: str, path: Path) -> None:
    """Persist the resolved *path* for *model_id* in the cache."""
    data = _load()
    data[model_id] = str(path)
    _save(data)


def get_model_path(model_id: str) -> Path | None:
    """Return the cached path for *model_id*, or None if missing/stale."""
    val = _load().get(model_id)
    if val:
        p = Path(val)
        if p.exists():
            return p
    return None
