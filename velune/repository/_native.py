"""Optional bridge to the Rust native extension (velune_native).

Exposes the same API whether or not the compiled extension is available:

    sha256_file(path: str) -> str
    scan_directory(root: str, extensions: list[str], skip_names: list[str]) -> list[str]

When the Rust wheel is installed (``pip install velune-native``), calls are
delegated to the compiled C-extension.  When it is not available the functions
fall back to pure-Python implementations so the rest of the codebase never
needs to guard for the import.

Performance note (see scripts/benchmark_native.py):
  Python's hashlib.sha256 is already OpenSSL-backed (~1.4 GB/s on large files).
  Rust's sha2 crate without explicit SIMD flags is unlikely to beat it.
  sha256_file is kept here as a clean, unified interface — the Python fallback
  is the correct default until a benchmark on the target release platform proves
  Rust wins.  scan_directory is where Rust is more likely to provide real gains
  for large repos (reduced per-entry Python overhead over os.walk).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = ["sha256_file", "scan_directory", "NATIVE_AVAILABLE"]

# ─── Try to load the Rust extension ──────────────────────────────────────────

try:
    import velune_native as _rust  # type: ignore[import-not-found]

    NATIVE_AVAILABLE: bool = True
except ImportError:
    _rust = None  # type: ignore[assignment]
    NATIVE_AVAILABLE = False


# ─── Public API ───────────────────────────────────────────────────────────────


def sha256_file(path: str | Path) -> str:
    """Return the SHA-256 hex digest of the file at *path*.

    Uses the Rust native extension when available, otherwise falls back to
    ``hashlib``.  Raises ``OSError`` when the file cannot be read.
    """
    path = str(path)
    if NATIVE_AVAILABLE:
        return _rust.sha256_file(path)
    return _sha256_file_py(path)


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
    root = str(root)
    if NATIVE_AVAILABLE:
        return _rust.scan_directory(root, extensions, skip_names)
    return _scan_directory_py(root, extensions, skip_names)


# ─── Pure-Python fallbacks ────────────────────────────────────────────────────


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
