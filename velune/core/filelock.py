"""Minimal cross-platform advisory file lock.

Guards a critical section shared by more than one *process* — the
in-process ``threading.Lock``s already used elsewhere in this codebase (e.g.
``CredentialManager._cache_lock``) do nothing once a second Velune process
opens the same file, and ``credentials.json``'s read-merge-write save cycle
otherwise has a real lost-update race between two terminal sessions running
``/providers add`` concurrently.

No new dependency: POSIX uses ``fcntl.flock``, Windows uses
``msvcrt.locking``. Both are stdlib. This is advisory locking only — it does
nothing against a process that doesn't use it, which is fine here since
every writer of ``credentials.json`` lives in this codebase.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path

_log = logging.getLogger("velune.core.filelock")

_DEFAULT_TIMEOUT = 5.0
_POLL_INTERVAL = 0.05


class FileLockTimeout(TimeoutError):
    """Raised when a lock could not be acquired within the given timeout."""


class FileLock:
    """An advisory, re-entrant-per-process lock backed by a sidecar ``.lock`` file.

    ``target`` is the file being protected (e.g. ``credentials.json``) — the
    actual lock is taken on ``target`` + ``.lock`` so the protected file's own
    atomic temp-file-replace dance is never touched by this class.
    """

    def __init__(self, target: Path | str) -> None:
        self._lock_path = Path(str(target) + ".lock")
        self._fh = None
        self._depth = 0

    def acquire(self, timeout: float = _DEFAULT_TIMEOUT) -> None:
        if self._depth > 0:
            # Re-entrant within the same process/thread stack (e.g. a save
            # that internally calls another locked helper) — just nest.
            self._depth += 1
            return

        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._lock_path, "a+b")
        deadline = time.monotonic() + timeout
        try:
            while True:
                if _try_lock(fh):
                    self._fh = fh
                    self._depth = 1
                    return
                if time.monotonic() >= deadline:
                    fh.close()
                    raise FileLockTimeout(
                        f"Could not acquire lock on {self._lock_path} within {timeout}s "
                        "(another Velune process may be writing credentials right now)."
                    )
                time.sleep(_POLL_INTERVAL)
        except BaseException:
            with contextlib.suppress(Exception):
                fh.close()
            raise

    def release(self) -> None:
        if self._depth == 0:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        fh, self._fh = self._fh, None
        if fh is not None:
            with contextlib.suppress(Exception):
                _unlock(fh)
            with contextlib.suppress(Exception):
                fh.close()

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def _try_lock(fh) -> bool:
    """Attempt a non-blocking exclusive lock on *fh*. Returns True on success."""
    if os.name == "nt":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    else:
        import fcntl

        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False


def _unlock(fh) -> None:
    if os.name == "nt":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(target: Path | str, timeout: float = _DEFAULT_TIMEOUT):
    """``with locked(path): ...`` — convenience wrapper around :class:`FileLock`."""
    lock = FileLock(target)
    lock.acquire(timeout=timeout)
    try:
        yield
    finally:
        lock.release()
