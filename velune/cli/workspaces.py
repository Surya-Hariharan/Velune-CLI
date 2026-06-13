"""Multi-project workspace registry and live workspace switching.

Each project Velune opens becomes a persistent *workspace*: its cognitive
core (SQLite), vector stores, sessions, and repository graph are already
isolated per-workspace on disk via :func:`velune.core.paths.workspace_storage_dir`.
This module adds the two missing pieces:

* :class:`WorkspaceRegistry` — a small JSON index at
  ``~/.velune/workspaces.json`` remembering every project Velune has worked
  in (name, path, last opened, detected project type, git status) so the
  ``/project`` picker can list and reopen them instantly.

* :func:`switch_workspace` — rebinds all workspace-scoped runtime services
  (storage pools, memory tiers, repository cognition) to a new root *inside
  the running process*, using the same module factories that built them at
  bootstrap. Old storage handles are shut down first so no connections leak.
  Cross-project memory bleed is prevented structurally: every store opens
  under the new workspace's own storage directory.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_log = logging.getLogger("velune.cli.workspaces")

DEFAULT_REGISTRY_PATH = Path.home() / ".velune" / "workspaces.json"

# Container keys whose instances are bound to a workspace root and must be
# rebuilt on switch. Anything not listed here (providers, model registry,
# council, hardware profile) is workspace-independent and survives untouched —
# that is what makes switching fast.
_WORKSPACE_SCOPED_KEYS = frozenset(
    {
        "runtime.sqlite_pool",
        "runtime.episodic_memory",
        "runtime.semantic_memory",
        "runtime.graph_memory",
        "runtime.lineage_memory",
        "runtime.lancedb_store",
        "runtime.embedding_pipeline",
        "runtime.semantic_memory_lance",
        "runtime.episodic_session_memory",
        "runtime.memory_lifecycle",
        "runtime.repository_cognition",
    }
)


@dataclass(slots=True)
class WorkspaceInfo:
    """One registered project workspace."""

    name: str
    path: str
    last_opened: str
    is_git: bool = False
    project_type: str | None = None


class WorkspaceRegistry:
    """Persistent index of every project workspace Velune has opened."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_REGISTRY_PATH
        self._entries: dict[str, WorkspaceInfo] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for raw in data.get("workspaces", []):
                known = set(WorkspaceInfo.__dataclass_fields__)
                info = WorkspaceInfo(**{k: v for k, v in raw.items() if k in known})
                self._entries[self._key(info.path)] = info
        except Exception as exc:
            _log.warning("Could not load workspace registry: %s", exc)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"workspaces": [asdict(w) for w in self._entries.values()]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def _key(path: str | Path) -> str:
        try:
            return str(Path(path).resolve()).lower()
        except Exception:
            return str(path).lower()

    # ── Public API ───────────────────────────────────────────────────────

    def register(self, path: Path) -> WorkspaceInfo:
        """Register (or refresh) *path* as a known project workspace."""
        resolved = path.resolve()
        info = WorkspaceInfo(
            name=resolved.name or str(resolved),
            path=str(resolved),
            last_opened=datetime.now().isoformat(timespec="seconds"),
            is_git=(resolved / ".git").exists(),
            project_type=self._detect_project_type(resolved),
        )
        self._entries[self._key(resolved)] = info
        self._save()
        return info

    def touch(self, path: Path) -> None:
        """Update the last-opened timestamp, registering if unknown."""
        key = self._key(path)
        if key in self._entries:
            self._entries[key].last_opened = datetime.now().isoformat(timespec="seconds")
            self._save()
        else:
            self.register(path)

    def get(self, path: Path) -> WorkspaceInfo | None:
        return self._entries.get(self._key(path))

    def find_by_name(self, name: str) -> WorkspaceInfo | None:
        lowered = name.lower()
        for info in self._entries.values():
            if info.name.lower() == lowered:
                return info
        return None

    def list(self) -> list[WorkspaceInfo]:
        """All workspaces, most recently opened first; prunes deleted paths."""
        alive = [w for w in self._entries.values() if Path(w.path).exists()]
        return sorted(alive, key=lambda w: w.last_opened, reverse=True)

    def remove(self, name: str) -> bool:
        info = self.find_by_name(name)
        if info is None:
            return False
        self._entries.pop(self._key(info.path), None)
        self._save()
        return True

    @staticmethod
    def _detect_project_type(path: Path) -> str | None:
        try:
            from velune.repository.project_type import ProjectTypeDetector

            profile = ProjectTypeDetector().detect(path)
            if profile is None:
                return None
            if isinstance(profile, dict):
                return profile.get("display_name")
            return getattr(profile, "display_name", None)
        except Exception:
            return None


async def switch_workspace(container: Any, new_workspace: Path) -> list[str]:
    """Rebind all workspace-scoped services to *new_workspace* in place.

    Returns human-readable notes about what happened (for the REPL to show).
    The sequence is: close old storage handles → hot-swap rebuilt services →
    initialize the new ones. Failures on individual optional services are
    logged and reported, never fatal — the switch always completes with the
    container in a consistent state for the new root.
    """
    notes: list[str] = []
    new_workspace = new_workspace.resolve()

    # 1. Shut down old storage-owning services so file handles and worker
    #    tasks never leak across workspaces.
    for key in ("runtime.embedding_pipeline", "runtime.lancedb_store", "runtime.sqlite_pool"):
        try:
            if container.has(key):
                old = container.get(key)
                if hasattr(old, "shutdown"):
                    await old.shutdown()
        except Exception as exc:
            _log.warning("Shutdown of %s during workspace switch failed: %s", key, exc)
            notes.append(f"warning: {key} did not shut down cleanly")

    # 2. Rebind the workspace root itself.
    container.register_instance("runtime.workspace", new_workspace)

    # 3. Rebuild workspace-scoped modules with the same factories used at
    #    bootstrap, in their declared dependency order.
    from velune.kernel.bootstrap import RuntimeEnvironment
    from velune.memory.module import MEMORY_MODULES
    from velune.repository.module import REPOSITORY_MODULES

    env = RuntimeEnvironment(
        workspace=new_workspace,
        config=container.get("runtime.config"),
        container=container,
        lifecycle=container.get("runtime.lifecycle"),
    )

    rebuilt = 0
    for module in MEMORY_MODULES + REPOSITORY_MODULES:
        if module.container_key not in _WORKSPACE_SCOPED_KEYS:
            continue
        try:
            instance = module.factory(env)
            container.hot_swap(module.container_key, instance)
            if hasattr(instance, "initialize"):
                await instance.initialize()
            rebuilt += 1
        except Exception as exc:
            _log.warning("Workspace switch: module '%s' failed to rebuild: %s", module.name, exc)
            notes.append(f"warning: {module.name} unavailable in this workspace")

    notes.insert(0, f"{rebuilt} workspace services rebound")
    return notes
