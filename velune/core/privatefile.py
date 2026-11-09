"""Create files that hold secrets so they are private from the moment they exist.

Writing a secret with a plain ``open()`` and tightening the permissions afterwards leaves a
window in which the file has the process's default mode (typically world-readable on POSIX, or the
parent folder's inherited ACL on Windows). These helpers close that window:

* the file is created exclusively (``O_EXCL``) with mode ``0o600``, after removing any stale file
  or symlink at that name, so a leftover or planted entry is never reused or followed;
* the Windows ACL is restricted *before* any data is written, and ``icacls`` is run from the
  system directory rather than being found by name.

Restricting permissions stays best-effort on Windows (a filesystem without ACLs must not block a
credential save); on POSIX a failed ``chmod`` is an error.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

logger = logging.getLogger("velune.core.privatefile")

__all__ = ["open_private", "restrict_to_current_user", "system_executable", "write_private_bytes"]


def system_executable(name: str) -> str | None:
    """Absolute path of a Windows system tool (``icacls``), or None when it is not there.

    Resolved from ``%SystemRoot%\\System32`` instead of by bare name, because Windows searches the
    current directory when a name has no path.
    """
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    candidate = Path(root) / "System32" / f"{name}.exe"
    return str(candidate) if candidate.is_file() else None


def restrict_to_current_user(path: Path) -> None:
    """Make *path* readable and writable by the current user only (``chmod 600`` / an ACL)."""
    if not path.exists():
        return
    if os.name != "nt":
        path.chmod(0o600)
        return
    import subprocess

    user = os.environ.get("USERNAME", "")
    icacls = system_executable("icacls")
    if not user or icacls is None:
        return
    domain = os.environ.get("USERDOMAIN", "")
    account = f"{domain}\\{user}" if domain else user
    try:
        subprocess.run(
            [icacls, str(path), "/inheritance:r", "/grant:r", f"{account}:F"],
            shell=False,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        logger.debug("Could not restrict ACL on %s (non-fatal)", path, exc_info=True)


@contextmanager
def open_private(path: Path) -> Iterator[BinaryIO]:
    """Open *path* for binary writing as a brand-new file only the current user can read.

    Any existing file, or symlink, at *path* is removed first and never written through.
    """
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        handle = os.fdopen(fd, "wb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        # Windows ignores the mode above; restrict the (still empty) file before any secret lands.
        restrict_to_current_user(path)
        yield handle


def write_private_bytes(path: Path, data: bytes) -> None:
    """Atomically replace *path* with *data*, private throughout (temp file, fsync, rename)."""
    path = Path(path)
    temp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open_private(temp) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass
