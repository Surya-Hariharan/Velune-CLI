"""SessionUsageTracker must not retain SQLite handles after an operation returns.

``with sqlite3.connect(...)`` only commits or rolls back; it never closes. Every
call used to leave a connection for the garbage collector, which showed up as
``ResourceWarning: unclosed database`` and, on Windows, ``WinError 32`` when a
sandbox tried to delete the database straight afterwards.
"""

from __future__ import annotations

import gc
import sqlite3
import threading
import warnings
from pathlib import Path

import pytest

from velune.telemetry import usage_tracker as ut
from velune.telemetry.usage_tracker import SessionUsageTracker, UsageEvent


class _Spy:
    """Wrap ``sqlite3.connect`` and remember every connection it hands out."""

    def __init__(self, real):
        self._real = real
        self.opened: list[sqlite3.Connection] = []
        self._lock = threading.Lock()

    def __call__(self, *args, **kwargs):
        conn = self._real(*args, **kwargs)
        with self._lock:
            self.opened.append(conn)
        return conn

    def open_handles(self) -> int:
        n = 0
        for conn in self.opened:
            try:
                conn.execute("SELECT 1")
            except sqlite3.ProgrammingError:
                continue
            n += 1
        return n


@pytest.fixture
def spy(monkeypatch):
    s = _Spy(sqlite3.connect)
    monkeypatch.setattr(ut.sqlite3, "connect", s)
    return s


@pytest.fixture
def tracker(tmp_path, spy) -> SessionUsageTracker:
    return SessionUsageTracker(tmp_path / "telemetry" / "usage.db")


def _record(tracker: SessionUsageTracker, session: str = "s1", n: int = 1) -> None:
    for _ in range(n):
        tracker.record_completion(session, "gpt-4o", 10, 5, provider_id="openai")


def test_every_operation_closes_its_connection(tracker, spy):
    _record(tracker)
    tracker.get_session_total_tokens("s1")
    tracker.get_session_estimated_cost("s1")
    tracker.get_session_summary("s1")
    tracker.get_provider_usage()
    tracker.get_model_usage()
    tracker.get_total_cost()
    tracker.get_cache_stats()
    tracker.get_recent_sessions()
    tracker.get_stats_last_n_days()
    tracker.cleanup_old_records(days=0)

    assert spy.opened, "the tracker should have opened connections"
    assert spy.open_handles() == 0


def test_init_closes_its_connection(tmp_path, spy):
    SessionUsageTracker(tmp_path / "usage.db")
    assert spy.opened and spy.open_handles() == 0


def test_record_event_closes_its_connection(tracker, spy):
    tracker.record_event(
        UsageEvent(
            session_id="s1",
            timestamp="2026-01-01T00:00:00",
            provider_id="openai",
            model_id="gpt-4o",
            input_tokens=1,
            output_tokens=1,
            latency_ms=1.0,
            success=True,
            error_code=None,
            cost_usd=0.0,
        )
    )
    assert spy.open_handles() == 0


def test_connection_is_closed_when_the_statement_fails(tracker, spy):
    with sqlite3.connect(tracker.db_path) as raw:
        raw.execute("DROP TABLE usage_records")
    raw.close()

    with pytest.raises(sqlite3.OperationalError):
        tracker.get_session_total_tokens("s1")
    with pytest.raises(sqlite3.OperationalError):
        _record(tracker)
    with pytest.raises(sqlite3.OperationalError):
        tracker.cleanup_old_records()

    assert spy.open_handles() == 0


def test_failed_write_is_rolled_back_and_leaves_the_database_usable(tracker):
    _record(tracker, n=2)

    # A NOT NULL violation (session_id=None) fails after the connection is open.
    with pytest.raises(sqlite3.IntegrityError):
        tracker.record_completion(None, "gpt-4o", 1, 1)  # type: ignore[arg-type]

    assert tracker.get_session_summary("s1").record_count == 2
    _record(tracker)
    assert tracker.get_session_summary("s1").record_count == 3


def test_repeated_operations_do_not_accumulate_handles(tracker, spy):
    _record(tracker, n=50)
    for _ in range(50):
        tracker.get_session_total_tokens("s1")
    assert len(spy.opened) >= 100
    assert spy.open_handles() == 0
    assert tracker.get_session_total_tokens("s1") == 50 * 15


def test_database_can_be_deleted_immediately_after_operations(tmp_path):
    db = tmp_path / "usage.db"
    tracker = SessionUsageTracker(db)
    _record(tracker, n=3)
    tracker.get_session_summary("s1")
    tracker.get_provider_usage()

    # WinError 32 on Windows if any handle is still open.
    for f in (db, Path(f"{db}-wal"), Path(f"{db}-shm"), Path(f"{db}-journal")):
        f.unlink(missing_ok=True)
    assert not db.exists()


def test_tmp_directory_can_be_removed_immediately(tmp_path):
    import shutil

    root = tmp_path / "telemetry"
    tracker = SessionUsageTracker(root / "usage.db")
    _record(tracker)
    tracker.get_total_cost()
    shutil.rmtree(root)
    assert not root.exists()


def test_no_resource_warning_under_default_filters(tmp_path):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("default")
        tracker = SessionUsageTracker(tmp_path / "usage.db")
        _record(tracker, n=5)
        tracker.get_session_summary("s1")
        tracker.get_recent_sessions()
        del tracker
        gc.collect()

    leaked = [w for w in caught if issubclass(w.category, ResourceWarning)]
    assert not leaked, [str(w.message) for w in leaked]


def test_concurrent_writers_and_readers_stay_correct_and_leave_nothing_open(tmp_path, spy):
    tracker = SessionUsageTracker(tmp_path / "usage.db")
    errors: list[BaseException] = []

    def writer():
        try:
            for _ in range(25):
                tracker.record_completion("s1", "gpt-4o", 10, 5, provider_id="openai")
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def reader():
        try:
            for _ in range(25):
                tracker.get_session_total_tokens("s1")
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert not errors
    assert tracker.get_session_total_tokens("s1") == 4 * 25 * 15
    assert spy.open_handles() == 0


def test_opens_a_database_written_by_an_older_schema(tmp_path, spy):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as raw:
        raw.execute(
            "CREATE TABLE usage_records (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL,"
            " timestamp TEXT NOT NULL, model TEXT NOT NULL, input_tokens INTEGER NOT NULL"
            " DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, total_tokens INTEGER NOT"
            " NULL DEFAULT 0, estimated_cost REAL)"
        )
        raw.execute(
            "INSERT INTO usage_records (session_id, timestamp, model, total_tokens)"
            " VALUES ('old', '2026-01-01T00:00:00', 'm', 7)"
        )
    raw.close()

    tracker = SessionUsageTracker(db)
    assert tracker.get_session_total_tokens("old") == 7
    _record(tracker, "new")
    assert tracker.get_session_total_tokens("new") == 15
    assert spy.open_handles() == 0
