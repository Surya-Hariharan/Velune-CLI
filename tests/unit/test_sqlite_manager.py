"""Unit tests for SQLiteManager write queue correctness and error safety (Batch 13)."""

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
# Tests
# ---------------------------------------------------------------------------


def test_write_sync_propagates_timeout(tmp_path: Path) -> None:
    """TimeoutError must be raised when the write thread stalls."""
    manager = _make_manager(tmp_path)

    # Stop the background thread so nothing is ever dequeued.
    manager._is_running = False
    manager._write_thread.join(timeout=2.0)

    # Patch done.wait to expire immediately so the test doesn't take 10 s.
    original_wait = threading.Event.wait

    def fast_wait(self: threading.Event, timeout: float | None = None) -> bool:
        return False  # simulate timeout

    with patch.object(threading.Event, "wait", fast_wait):
        with pytest.raises(TimeoutError) as exc_info:
            manager.execute_write_sync("INSERT INTO t VALUES (1)", ())

    msg = str(exc_info.value)
    assert "Queue depth" in msg or "queue" in msg.lower()


def test_execute_write_sync_propagates_errors(tmp_path: Path) -> None:
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


def test_execute_write_sync_success(tmp_path: Path) -> None:
    """No exception must be raised for a valid DDL + DML roundtrip."""
    manager = _make_manager(tmp_path)

    # Schema
    manager.execute_script("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
    # Synchronous write
    manager.execute_write_sync("INSERT INTO t (id) VALUES (?)", (42,))

    rows = manager.execute_read("SELECT id FROM t")
    assert len(rows) == 1
    assert rows[0]["id"] == 42


def test_fire_and_forget_does_not_raise(tmp_path: Path) -> None:
    """execute_write (fire-and-forget) must not raise; errors are only logged."""
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


def test_execute_read_returns_rows(tmp_path: Path) -> None:
    """Verify that execute_read queries successfully retrieve sqlite3.Row results."""
    manager = _make_manager(tmp_path)
    manager.execute_script("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, content TEXT)")
    manager.execute_write_sync("INSERT INTO notes (content) VALUES (?)", ("Hello World",))
    
    rows = manager.execute_read("SELECT * FROM notes")
    assert len(rows) == 1
    assert rows[0]["content"] == "Hello World"
    assert rows[0]["id"] == 1


def test_write_thread_survives_error(tmp_path: Path) -> None:
    """The write thread must remain alive after a write failure."""
    manager = _make_manager(tmp_path)

    # Create schema first so subsequent write has a valid target.
    manager.execute_script("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")

    call_count = 0
    original_do_write = manager._do_write

    def fail_once(query: str, params: object) -> None:
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
    assert 2 in ids
