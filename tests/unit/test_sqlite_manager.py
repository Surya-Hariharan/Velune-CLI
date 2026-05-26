"""Unit tests for SQLiteManager write error propagation (Batch 04)."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from velune.memory.storage.sqlite_manager import SQLiteManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(tmp_path: Path) -> SQLiteManager:
    """Return a live SQLiteManager backed by a temp file."""
    return SQLiteManager(tmp_path / "test.db")


# ---------------------------------------------------------------------------
# Test 1 — execute_write_sync raises TimeoutError when queue is never drained
# ---------------------------------------------------------------------------


def test_write_sync_propagates_timeout(tmp_path: Path) -> None:
    """TimeoutError must be raised (not swallowed) when the write thread stalls."""
    manager = _make_manager(tmp_path)

    # Stop the background thread so nothing is ever dequeued.
    manager._is_running = False
    manager._write_thread.join(timeout=2.0)

    # Patch done.wait to expire immediately so the test doesn't take 10 s.
    original_wait = threading.Event.wait

    def fast_wait(self: threading.Event, timeout: float | None = None) -> bool:  # noqa: ANN001
        return False  # simulate timeout

    with patch.object(threading.Event, "wait", fast_wait):
        with pytest.raises(TimeoutError) as exc_info:
            manager.execute_write_sync("INSERT INTO t VALUES (1)", ())

    msg = str(exc_info.value)
    assert "Queue depth" in msg or "queue" in msg.lower(), (
        "TimeoutError message should contain queue depth info"
    )


# ---------------------------------------------------------------------------
# Test 2 — execute_write_sync propagates DB errors as RuntimeError
# ---------------------------------------------------------------------------


def test_write_sync_propagates_db_error(tmp_path: Path) -> None:
    """RuntimeError must surface when _do_write raises sqlite3.OperationalError."""
    manager = _make_manager(tmp_path)

    with patch.object(
        manager,
        "_do_write",
        side_effect=sqlite3.OperationalError("disk full"),
    ):
        with pytest.raises(RuntimeError) as exc_info:
            manager.execute_write_sync("INSERT INTO t VALUES (1)", ())

    assert "disk full" in str(exc_info.value).lower() or "SQLite write failed" in str(
        exc_info.value
    )


# ---------------------------------------------------------------------------
# Test 3 — execute_write_sync happy path (real in-memory equivalent)
# ---------------------------------------------------------------------------


def test_write_sync_success_path(tmp_path: Path) -> None:
    """No exception must be raised for a valid DDL + DML roundtrip."""
    manager = _make_manager(tmp_path)

    # Schema
    manager.execute_script("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
    # Synchronous write
    manager.execute_write_sync("INSERT INTO t (id) VALUES (?)", (42,))

    rows = manager.execute_read("SELECT id FROM t")
    assert len(rows) == 1
    assert rows[0]["id"] == 42


# ---------------------------------------------------------------------------
# Test 4 — execute_write (fire-and-forget) never raises even on DB error
# ---------------------------------------------------------------------------


def test_fire_and_forget_still_works(tmp_path: Path) -> None:
    """execute_write must not raise; errors are only logged."""
    manager = _make_manager(tmp_path)

    with patch.object(
        manager,
        "_do_write",
        side_effect=sqlite3.OperationalError("simulated failure"),
    ):
        # Must NOT raise
        manager.execute_write("INSERT INTO nonexistent VALUES (1)", ())

    # Give the write thread a moment to process the item.
    time.sleep(0.2)
    # If we reach here without an exception the test passes.


# ---------------------------------------------------------------------------
# Test 5 — write thread survives an error; subsequent writes succeed
# ---------------------------------------------------------------------------


def test_write_thread_survives_errors(tmp_path: Path) -> None:
    """The write thread must remain alive after a write failure."""
    manager = _make_manager(tmp_path)

    # Create schema first so subsequent write has a valid target.
    manager.execute_script("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")

    call_count = 0
    original_do_write = manager._do_write

    def fail_once(query: str, params: object) -> None:  # noqa: ANN001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise sqlite3.OperationalError("transient failure")
        original_do_write(query, params)

    with patch.object(manager, "_do_write", side_effect=fail_once):
        # First call — expect RuntimeError due to patched failure.
        with pytest.raises(RuntimeError):
            manager.execute_write_sync("INSERT INTO t VALUES (1)", ())

    # Thread must still be alive.
    assert manager._write_thread.is_alive(), "Write thread must survive a write error"

    # Subsequent write should succeed (patch is no longer active).
    manager.execute_write_sync("INSERT INTO t VALUES (2)", ())
    rows = manager.execute_read("SELECT id FROM t ORDER BY id")
    ids = [r["id"] for r in rows]
    assert 2 in ids, f"Expected row 2 after recovery, got: {ids}"
