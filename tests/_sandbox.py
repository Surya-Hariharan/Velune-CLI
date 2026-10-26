"""Create and remove the throwaway home the test suite runs in.

Kept free of ``velune`` imports so ``tests/conftest.py`` can use it before any
application module is imported, and so the tests can exercise it directly.
"""

from __future__ import annotations

import gc
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path

SANDBOX_PREFIX = "velune-test-home-"
MARKER_NAME = ".velune-test-sandbox"
_FALLBACK_BASE_NAME = "velune-test-sandboxes"


def _has_appdata(path: Path) -> bool:
    return any(part.lower() == "appdata" for part in path.parts)


def sandbox_base() -> Path:
    """Directory that holds sandbox roots.

    Windows' %TEMP% lives under ``...\\AppData\\Local``. A sandbox there makes every
    home-derived path contain "AppData", which blinds tests that check the
    ``%LOCALAPPDATA%``-derived paths apart from the home-derived ones. Prefer a
    base whose path has no such component; ``VELUNE_TEST_SANDBOX_BASE`` overrides.
    """
    override = os.environ.get("VELUNE_TEST_SANDBOX_BASE")
    if override:
        return Path(override).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if not _has_appdata(temp):
        return temp
    public = os.environ.get("PUBLIC")
    if public:
        candidate = Path(public) / _FALLBACK_BASE_NAME
        try:
            candidate.mkdir(exist_ok=True)
            return candidate.resolve()
        except OSError:
            pass
    return temp


def create_sandbox(base: Path | None = None) -> tuple[Path, Path]:
    """Return ``(root, base)``; ``root`` is a fresh directory carrying an ownership marker."""
    base = (base or sandbox_base()).resolve()
    root = Path(tempfile.mkdtemp(prefix=SANDBOX_PREFIX, dir=base)).resolve()
    (root / MARKER_NAME).write_text(f"{os.getpid()}:{secrets.token_hex(8)}\n", encoding="utf-8")
    return root, base


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(os.path, "isjunction", None)
    return bool(is_junction and is_junction(path))


def ownership_problem(root: Path, base: Path) -> str | None:
    """Why ``root`` must not be deleted, or ``None`` when it is exactly our sandbox."""
    if not root.name.startswith(SANDBOX_PREFIX):
        return f"name {root.name!r} lacks the {SANDBOX_PREFIX!r} prefix"
    if root.parent != base:
        return f"parent {root.parent} is not the sandbox base {base}"
    if _is_link(root) or root.resolve() != root:
        return "root is a link or resolves elsewhere"
    marker = root / MARKER_NAME
    if not marker.is_file():
        return "ownership marker is missing"
    if not marker.read_text(encoding="utf-8").startswith(f"{os.getpid()}:"):
        return "ownership marker belongs to another process"
    return None


def release_handles() -> None:
    """Drop what could still hold the sandbox open: the usage-tracker singleton, then GC.

    ``with sqlite3.connect(...)`` commits but does not close, so a connection kept
    alive by a stray reference keeps its file locked on Windows until collected.
    """
    module = sys.modules.get("velune.telemetry.usage_tracker")
    if module is not None and hasattr(module, "_tracker"):
        module._tracker = None
    gc.collect()


def remove_sandbox(
    root: Path, base: Path, *, attempts: int = 5, delay: float = 0.2
) -> list[tuple[str, str]]:
    """Delete ``root`` after verifying ownership; return ``(path, error)`` for what remains.

    Raises ``RuntimeError`` rather than deleting when ownership checks fail.
    """
    problem = ownership_problem(root, base)
    if problem:
        raise RuntimeError(f"refusing to remove {root}: {problem}")
    errors: list[tuple[str, str]] = []

    def record(_func, path, exc):
        error = exc[1] if isinstance(exc, tuple) else exc
        errors.append((str(path), f"{type(error).__name__}: {error}"))

    for attempt in range(attempts):
        release_handles()
        errors.clear()
        if sys.version_info >= (3, 12):
            shutil.rmtree(root, onexc=record)
        else:
            shutil.rmtree(root, onerror=record)
        if not root.exists():
            break
        time.sleep(delay * (attempt + 1))
    if root.exists() and not errors:
        errors.append((str(root), "directory still exists after removal"))
    if not root.exists():
        _drop_empty_fallback_base(base)
        return []
    return errors


def _drop_empty_fallback_base(base: Path) -> None:
    # Only the base this module created, and only when nothing else is in it.
    if base.name == _FALLBACK_BASE_NAME:
        try:
            base.rmdir()
        except OSError:
            pass
