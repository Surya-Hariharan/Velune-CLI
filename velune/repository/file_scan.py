"""File-system helpers for the repository indexers.

    sha256_file(path: str | Path) -> str
    scan_directory(root, extensions, skip_names) -> list[str]

Both are plain Python (``hashlib`` and ``os.walk``).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = ["sha256_file", "scan_directory"]

# ─── Public API ───────────────────────────────────────────────────────────────


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 hex digest of the file at *path*.

    Raises ``OSError`` when the file cannot be read.
    """
    return _sha256_file_py(str(path))


def scan_directory(
    root: str | Path,
    extensions: list[str],
    skip_names: list[str],
) -> list[str]:
    """Walk *root* and return sorted absolute paths matching *extensions*.

    Args:
        root: Directory to walk.
        extensions: File extensions to include (e.g. ``[".py", ".rs"]``).
                    Empty list means include all files.
        skip_names: Directory names to prune entirely (e.g. ``[".venv", "node_modules"]``).

    Returns:
        Sorted list of absolute path strings.
    """
    return _scan_directory_py(str(root), extensions, skip_names)


# ─── Implementations ────────────────────────────────────────────────────────


def _sha256_file_py(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _scan_directory_py(
    root: str,
    extensions: list[str],
    skip_names: list[str],
) -> list[str]:
    ext_lower = {e.lower() for e in extensions}
    skip_set = set(skip_names)
    results: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_set]
        for name in filenames:
            if not ext_lower or Path(name).suffix.lower() in ext_lower:
                results.append(os.path.join(dirpath, name))

    results.sort()
    return results
