"""Persistent registry of Ollama model storage locations.

Users routinely keep Ollama models off the system drive — on a second internal
disk, an external SSD, or a USB drive — because the models are huge. Ollama
itself supports this via the ``OLLAMA_MODELS`` environment variable, but that is
invisible to a tool unless it (a) honours the env var and (b) lets the user
register additional roots that persist across sessions. This module does both.

Locations are stored as JSON at ``~/.velune/model_locations.json`` using the
same atomic-write pattern as :mod:`velune.cli.model_prefs`. Resolution order for
*where to look for Ollama models* is:

    1. explicitly registered locations (this file)
    2. the ``OLLAMA_MODELS`` environment variable
    3. platform default install locations

A registered location whose drive is currently disconnected is reported as such
(not silently dropped), and is picked up automatically the moment the same
drive/mount reappears — no re-registration needed.
"""

from __future__ import annotations

import json
import logging
import os
import platform
from dataclasses import dataclass
from pathlib import Path

from velune.providers.ollama_store import OllamaModelStore

_log = logging.getLogger("velune.providers.ollama_locations")

DEFAULT_LOCATIONS_PATH = Path.home() / ".velune" / "model_locations.json"


@dataclass(slots=True)
class ModelLocation:
    """A registered model storage root."""

    path: str
    label: str = ""
    kind: str = "ollama"

    @property
    def resolved(self) -> Path:
        return Path(self.path).expanduser()


@dataclass(slots=True)
class ResolvedRoot:
    """A candidate Ollama root with live status, for display and discovery."""

    path: Path
    source: str  # "registered" | "env:OLLAMA_MODELS" | "default"
    label: str = ""

    @property
    def exists(self) -> bool:
        try:
            return self.path.exists()
        except OSError:
            return False

    @property
    def is_valid(self) -> bool:
        return OllamaModelStore.is_valid_root(self.path)

    @property
    def disconnected(self) -> bool:
        """Registered but unreachable — typically a removable/secondary drive."""
        return self.source == "registered" and not self.exists


@dataclass(slots=True)
class AddResult:
    ok: bool
    message: str
    location: ModelLocation | None = None


# ---------------------------------------------------------------------------
# Platform defaults
# ---------------------------------------------------------------------------


def _default_roots() -> list[Path]:
    """Well-known default Ollama install locations for this platform."""
    roots: list[Path] = [Path.home() / ".ollama" / "models"]
    system = platform.system()
    if system == "Windows":
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            roots.append(Path(userprofile) / ".ollama" / "models")
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            roots.append(Path(localappdata) / "Ollama" / "models")
    else:
        # Linux service installs commonly use this system path.
        roots.append(Path("/usr/share/ollama/.ollama/models"))
        roots.append(Path("/usr/share/ollama/models"))
    return roots


def _env_root() -> Path | None:
    """The ``OLLAMA_MODELS`` override, if set."""
    value = os.environ.get("OLLAMA_MODELS")
    return Path(value).expanduser() if value else None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class OllamaLocationRegistry:
    """Load/save and resolve registered Ollama model locations."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or DEFAULT_LOCATIONS_PATH

    # -- persistence ----------------------------------------------------

    def load(self) -> list[ModelLocation]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            items = data.get("locations", []) if isinstance(data, dict) else data
            out: list[ModelLocation] = []
            for item in items:
                if isinstance(item, dict) and item.get("path"):
                    out.append(
                        ModelLocation(
                            path=item["path"],
                            label=item.get("label", ""),
                            kind=item.get("kind", "ollama"),
                        )
                    )
            return out
        except Exception as exc:
            _log.warning("Could not read model locations: %s", exc)
            return []

    def _save(self, locations: list[ModelLocation]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "locations": [
                    {"path": loc.path, "label": loc.label, "kind": loc.kind} for loc in locations
                ]
            }
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception as exc:
            _log.warning("Could not persist model locations: %s", exc)

    # -- mutation -------------------------------------------------------

    @staticmethod
    def _key(path: Path | str) -> str:
        """Stable identity for duplicate detection (resolved, normalized)."""
        try:
            return str(Path(path).expanduser().resolve()).rstrip("\\/").lower()
        except Exception:
            return str(path).rstrip("\\/").lower()

    def add(self, path: Path | str, label: str = "") -> AddResult:
        """Validate and register *path*. Idempotent; reports clear diagnostics."""
        candidate = Path(path).expanduser()
        if not candidate.exists():
            return AddResult(False, f"Path does not exist: {candidate}")
        if not candidate.is_dir():
            return AddResult(False, f"Not a directory: {candidate}")
        if not OllamaModelStore.is_valid_root(candidate):
            return AddResult(
                False,
                f"Not an Ollama model store (no manifests/ + blobs/): {candidate}",
            )

        existing = self.load()
        key = self._key(candidate)
        for loc in existing:
            if self._key(loc.path) == key:
                return AddResult(True, f"Already registered: {candidate}", loc)

        location = ModelLocation(path=str(candidate), label=label, kind="ollama")
        existing.append(location)
        self._save(existing)
        return AddResult(True, f"Registered model location: {candidate}", location)

    def remove(self, path: Path | str) -> bool:
        existing = self.load()
        key = self._key(path)
        kept = [loc for loc in existing if self._key(loc.path) != key]
        if len(kept) == len(existing):
            return False
        self._save(kept)
        return True

    # -- resolution -----------------------------------------------------

    def resolve_roots(self) -> list[ResolvedRoot]:
        """Ordered, de-duplicated candidate roots with live status.

        Order: registered → ``OLLAMA_MODELS`` → platform defaults. Disconnected
        registered roots are kept (and flagged) so the caller can tell the user
        a drive is offline rather than silently showing no models.
        """
        seen: set[str] = set()
        out: list[ResolvedRoot] = []

        def _push(path: Path, source: str, label: str = "") -> None:
            key = self._key(path)
            if key in seen:
                return
            seen.add(key)
            out.append(ResolvedRoot(path=path, source=source, label=label))

        for loc in self.load():
            _push(loc.resolved, "registered", loc.label)
        env = _env_root()
        if env is not None:
            _push(env, "env:OLLAMA_MODELS")
        for root in _default_roots():
            _push(root, "default")
        return out

    def active_stores(self) -> list[OllamaModelStore]:
        """Stores for every currently-reachable, valid root (discovery input)."""
        return [
            OllamaModelStore(rr.path) for rr in self.resolve_roots() if rr.exists and rr.is_valid
        ]

    def disconnected(self) -> list[ResolvedRoot]:
        """Registered roots whose storage is currently unavailable."""
        return [rr for rr in self.resolve_roots() if rr.disconnected]
