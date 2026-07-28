"""Cross-process credential-write safety: velune/core/filelock.py.

Two terminal sessions racing on ``/providers add`` used to have a real
lost-update window — nothing in this codebase took an OS-level lock around
the credentials.json read-merge-write cycle. These tests exercise the lock
primitive itself (in-process, since a genuine second-process test would need
a subprocess harness); ``test_keystore.py`` covers that ``CredentialManager``
actually uses it.
"""

from __future__ import annotations

import time

import pytest

from velune.core.filelock import FileLock, FileLockTimeout, locked


def test_lock_excludes_a_second_holder(tmp_path):
    target = tmp_path / "credentials.json"
    first = FileLock(target)
    first.acquire(timeout=1.0)
    try:
        second = FileLock(target)
        with pytest.raises(FileLockTimeout):
            second.acquire(timeout=0.2)
    finally:
        first.release()


def test_lock_is_released_and_reacquirable(tmp_path):
    target = tmp_path / "credentials.json"
    with locked(target):
        pass
    # Must be acquirable again immediately — the previous holder released it.
    with locked(target, timeout=0.5):
        pass


def test_lock_is_reentrant_within_same_holder(tmp_path):
    target = tmp_path / "credentials.json"
    lock = FileLock(target)
    lock.acquire(timeout=1.0)
    try:
        # Nested acquire from the same FileLock instance must not deadlock.
        lock.acquire(timeout=1.0)
        lock.release()
    finally:
        lock.release()


def test_context_manager_releases_on_exception(tmp_path):
    target = tmp_path / "credentials.json"
    with pytest.raises(ValueError):
        with locked(target):
            raise ValueError("boom")
    # The lock must have been released despite the exception.
    with locked(target, timeout=0.5):
        pass


def test_second_lock_succeeds_once_first_releases(tmp_path):
    target = tmp_path / "credentials.json"
    first = FileLock(target)
    first.acquire(timeout=1.0)

    acquired_at = []

    def release_soon():
        time.sleep(0.1)
        first.release()

    import threading

    t = threading.Thread(target=release_soon)
    t.start()
    second = FileLock(target)
    start = time.monotonic()
    second.acquire(timeout=2.0)
    acquired_at.append(time.monotonic() - start)
    second.release()
    t.join()

    assert acquired_at[0] >= 0.09  # waited for the first holder to release
