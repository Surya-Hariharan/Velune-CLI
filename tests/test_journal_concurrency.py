"""M1: the lifecycle journal is created exclusively. Temp directories only; no uv, no app data.

Two lifecycle commands must never both believe they own the journal. These tests run real
concurrent writers (threads, and separate processes) and also force the exact interleaving
that broke the old check-then-replace code: every writer passes the "does it exist?" check
before any of them creates the file.
"""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from velune.lifecycle import journal as jr
from velune.lifecycle.errors import ExitCode, LifecycleError
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)  # every entry starts in the same second
OPERATIONS = ("update", "repair", "uninstall")


def confirmed() -> OwnershipReport:
    return OwnershipReport(
        kind=OwnershipKind.UV_TOOL,
        reasons=(),
        prefix="/t/velune-cli",
        version="0.9.9",
        specifier=None,
        extras=(),
        python_base=None,
    )


def make(operation: str = "update") -> jr.JournalEntry:
    return jr.new_entry(operation, confirmed(), python_version="3.12.4", now=NOW)


def race(path: Path, count: int) -> dict[str, object]:
    """Start *count* writers at the same instant. Returns ``{operation-index: outcome}``."""
    gate = threading.Barrier(count)
    outcomes: dict[str, object] = {}
    entries = [make(OPERATIONS[i % 3]) for i in range(count)]

    def worker(i: int) -> None:
        gate.wait(timeout=10)
        try:
            jr.write_journal(entries[i], path)
            outcomes[str(i)] = "won"
        except LifecycleError as exc:
            outcomes[str(i)] = exc.code
        except BaseException as exc:  # a raw exception would be a bug: record it
            outcomes[str(i)] = exc

    threads = [threading.Thread(target=worker, args=(i,), name=f"w{i}") for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    outcomes["tokens"] = [e.token for e in entries]
    return outcomes


def assert_one_winner(path: Path, outcomes: dict[str, object], count: int) -> None:
    results = [outcomes[str(i)] for i in range(count)]
    winners = [i for i, r in enumerate(results) if r == "won"]
    assert len(winners) == 1, results
    for r in results:
        assert r == "won" or r in (ExitCode.BUSY, ExitCode.REFUSED), results  # never a raw error
    read = jr.read_journal(path)
    assert read.state == "ok" and read.entry is not None
    assert read.entry.token == outcomes["tokens"][winners[0]]  # the file is the winner's
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name]  # no tmp, no lock file


# ---- real concurrency ------------------------------------------------------------------------


@pytest.mark.parametrize("round_", range(15))
def test_exactly_one_of_many_simultaneous_writers_wins(tmp_path, round_):
    path = tmp_path / "lifecycle" / "journal.json"
    path.parent.mkdir()
    assert_one_winner(path, race(path, 8), 8)


def test_every_writer_passes_the_existence_check_and_still_only_one_wins(tmp_path, monkeypatch):
    """The old code lost here: all writers see 'no journal', then each replaces the file."""
    path = tmp_path / "journal.json"
    count = 6
    gate = threading.Barrier(count)
    real_lexists = os.path.lexists

    def gated(p):
        seen = real_lexists(p)
        if str(p) == str(path) and threading.current_thread().name.startswith("w"):
            gate.wait(timeout=10)  # nobody continues until everybody has looked
        return seen

    monkeypatch.setattr(jr.os.path, "lexists", gated)
    outcomes = race(path, count)
    results = [outcomes[str(i)] for i in range(count)]
    assert results.count("won") == 1, results
    assert results.count(ExitCode.BUSY) == count - 1, results  # all lost on the creation itself
    assert_one_winner(path, outcomes, count)


_CHILD = r"""
import os, sys, time
from datetime import datetime, timezone
from velune.lifecycle import journal as jr
from velune.lifecycle.errors import LifecycleError
from velune.lifecycle.ownership import OwnershipKind, OwnershipReport

path, operation, go = sys.argv[1], sys.argv[2], sys.argv[3]
report = OwnershipReport(kind=OwnershipKind.UV_TOOL, reasons=(), prefix="/t/velune-cli",
                         version="0.9.9", specifier=None, extras=(), python_base=None)
entry = jr.new_entry(operation, report, python_version="3.12.4",
                     now=datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc))
print("READY", flush=True)
while not os.path.exists(go):
    time.sleep(0.0005)
try:
    jr.write_journal(entry, __import__("pathlib").Path(path))
    print("WON", entry.token)
except LifecycleError as exc:
    print("LOST", int(exc.code))
"""


@pytest.mark.parametrize("round_", range(4))
def test_two_separate_processes_cannot_both_create_the_journal(tmp_path, round_):
    path = tmp_path / "journal.json"
    go = tmp_path / "go"
    repo = str(Path(jr.__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=repo + os.pathsep + os.environ.get("PYTHONPATH", ""))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", _CHILD, str(path), op, str(go)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for op in ("update", "repair")
    ]
    for p in procs:  # wait until both are imported and spinning, then release them together
        assert p.stdout.readline().strip() == "READY"
    go.write_text("go")
    outputs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        outputs.append(out.strip())
    won = [o for o in outputs if o.startswith("WON")]
    lost = [o for o in outputs if o.startswith("LOST")]
    assert len(won) == 1 and len(lost) == 1, outputs
    assert lost[0].split()[1] in {str(int(ExitCode.BUSY)), str(int(ExitCode.REFUSED))}
    assert jr.read_journal(path).entry.token == won[0].split()[1]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["go", "journal.json"]


# ---- a journal that appears at the last moment is never overwritten ---------------------------


def test_a_journal_created_after_the_check_is_never_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    real_lexists = os.path.lexists

    def late(p):
        seen = real_lexists(p)
        if str(p) == str(path) and not seen:
            path.write_text("someone else got here first")  # between the check and the creation
        return seen

    monkeypatch.setattr(jr.os.path, "lexists", late)
    with pytest.raises(LifecycleError) as caught:
        jr.write_journal(make(), path)
    assert caught.value.code is ExitCode.BUSY and caught.value.details["reason"] == "journal_race"
    assert path.read_text() == "someone else got here first"
    assert [p.name for p in tmp_path.iterdir()] == ["journal.json"]  # our staging file is gone


# ---- interrupted writes and cleanup failures ---------------------------------------------------


def test_an_interrupted_write_leaves_no_journal_and_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "lifecycle" / "journal.json"

    def broken(*a, **k):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(jr.json, "dump", broken)
    with pytest.raises(OSError):
        jr.write_journal(make(), path)
    assert list(path.parent.iterdir()) == []
    assert jr.read_journal(path).state == "missing"


def test_a_failed_cleanup_after_success_does_not_fail_the_operation(tmp_path, monkeypatch):
    path = tmp_path / "journal.json"
    real_unlink = Path.unlink

    def stubborn(self, *a, **k):
        if self.name.startswith(".journal-"):
            raise PermissionError("antivirus holds the staging file")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", stubborn)
    entry = make()
    assert jr.write_journal(entry, path) == path  # no exception
    assert jr.read_journal(path).entry.token == entry.token
    advanced = jr.advance_phase(entry, "applying", path)  # cleanup failure here is harmless too
    assert jr.read_journal(path).entry.phase == "applying" and advanced.phase == "applying"


# ---- filesystems without hard links ------------------------------------------------------------


@pytest.fixture
def no_hard_links(monkeypatch):
    def refuse(*a, **k):
        raise OSError(errno.EPERM, "hard links are not supported here")

    monkeypatch.setattr(jr.os, "link", refuse)


def test_fallback_creates_a_complete_readable_journal(tmp_path, no_hard_links):
    path = tmp_path / "journal.json"
    entry = make()
    jr.write_journal(entry, path)
    read = jr.read_journal(path)
    assert read.state == "ok" and read.entry == entry and read.entry.token == entry.token
    assert [p.name for p in tmp_path.iterdir()] == ["journal.json"]


def test_fallback_is_still_exclusive_against_simultaneous_writers(tmp_path, no_hard_links):
    path = tmp_path / "journal.json"
    for _ in range(8):
        for leftover in tmp_path.iterdir():
            leftover.unlink()
        assert_one_winner(path, race(path, 6), 6)


def test_fallback_never_overwrites_a_late_journal(tmp_path, no_hard_links, monkeypatch):
    path = tmp_path / "journal.json"
    real_lexists = os.path.lexists

    def late(p):
        seen = real_lexists(p)
        if str(p) == str(path) and not seen:
            path.write_text("first")
        return seen

    monkeypatch.setattr(jr.os.path, "lexists", late)
    with pytest.raises(LifecycleError) as caught:
        jr.write_journal(make(), path)
    assert caught.value.code is ExitCode.BUSY and path.read_text() == "first"


def test_fallback_removes_its_own_partial_file_when_writing_fails(
    tmp_path, no_hard_links, monkeypatch
):
    path = tmp_path / "journal.json"
    real_fsync = os.fsync
    calls = []

    def flaky(fd):
        calls.append(fd)
        if len(calls) == 2:  # the first fsync is the staging file; the second is the target
            raise OSError(errno.EIO, "io error")
        return real_fsync(fd)

    monkeypatch.setattr(jr.os, "fsync", flaky)
    with pytest.raises(OSError):
        jr.write_journal(make(), path)
    assert list(tmp_path.iterdir()) == []  # nothing half-written is left to block anyone


# ---- operations that start in the same second stay distinguishable ----------------------------


def test_same_second_operations_have_different_tokens():
    a, b = make("update"), make("update")
    assert a.started_at == b.started_at and a.from_version == b.from_version
    assert a.token and b.token and a.token != b.token and len(a.token) == 32


def test_a_loser_cannot_advance_or_clear_the_winners_journal(tmp_path):
    path = tmp_path / "journal.json"
    winner, loser = make("update"), make("update")  # identical in every visible field
    jr.write_journal(winner, path)
    before = path.read_bytes()
    with pytest.raises(LifecycleError) as caught:
        jr.advance_phase(loser, "applying", path)
    assert caught.value.details["reason"] == "journal_not_ours"
    assert jr.clear_journal(loser, path) is False
    assert path.read_bytes() == before
    assert jr.advance_phase(winner, "applying", path).phase == "applying"
    assert jr.clear_journal(winner, path) is True


def test_token_round_trips_and_is_omitted_when_absent(tmp_path):
    from dataclasses import replace

    path = tmp_path / "journal.json"
    entry = make()
    jr.write_journal(entry, path)
    assert json.loads(path.read_text())["token"] == entry.token
    legacy = tmp_path / "legacy.json"
    jr.write_journal(replace(entry, token=None), legacy)
    assert "token" not in json.loads(legacy.read_text())


def test_a_journal_written_before_tokens_existed_still_works(tmp_path):
    path = tmp_path / "journal.json"
    entry = make()
    data = entry.to_json_dict()
    data.pop("token")
    path.write_text(json.dumps(data))
    read = jr.read_journal(path)
    assert read.state == "ok" and read.entry.token is None
    assert jr.advance_phase(read.entry, "applying", path).phase == "applying"
    assert jr.clear_journal(read.entry, path) is True


def test_a_non_string_token_is_malformed_not_a_crash(tmp_path):
    path = tmp_path / "journal.json"
    data = make().to_json_dict()
    data["token"] = 12345
    path.write_text(json.dumps(data))
    assert jr.read_journal(path).state == "malformed"


def test_no_lock_file_is_ever_created(tmp_path):
    path = tmp_path / "journal.json"
    entry = make()
    jr.write_journal(entry, path)
    jr.advance_phase(entry, "applying", path)
    assert [p.name for p in tmp_path.iterdir()] == ["journal.json"]
    time.sleep(0)  # (nothing to wait for: there is no lock to expire)
