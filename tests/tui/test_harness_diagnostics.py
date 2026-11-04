"""The ``wait_for`` failure report: informative, bounded, and free of secrets.

No PTY is started: a ``TermSession`` is built without its constructor and fed
chunks the way ``_pump`` would.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque

import pyte
import pytest

from . import harness
from .fake_backend import FAKE_KEY
from .harness import TermSession


def _session(monkeypatch, tmp_path) -> TermSession:
    monkeypatch.setattr(harness, "ARTIFACT_DIR", tmp_path / "artifacts")
    s = object.__new__(TermSession)
    s.screen = pyte.Screen(80, 10)
    s._stream = pyte.Stream(s.screen)
    s._lock = threading.Lock()
    s.raw = ""
    s._t0 = time.monotonic()
    s._tail = deque(maxlen=harness._TAIL_CHUNKS)
    s._chunk_count = 0
    s._last_output = None
    s._reader_end = None
    s._p = _FakePty()
    s._t = threading.Thread(target=lambda: None)  # never started: reads as "stopped"
    s.eof = False
    return s


class _FakePty:
    """Stands in for winpty/ptyprocess: scripted reads, no real terminal."""

    def __init__(self, *, alive=True, exitstatus=None, reads=(), **extra):
        self._alive, self.exitstatus, self._reads = alive, exitstatus, list(reads)
        for k, v in extra.items():
            setattr(self, k, v)

    def isalive(self):
        return self._alive

    def read(self, _size):
        item = self._reads.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _feed(s: TermSession, data: str) -> None:
    s.raw += data
    s._stream.feed(data)
    s._tail.append((time.monotonic() - s._t0, harness._clip(data)))
    s._chunk_count += 1
    s._last_output = time.monotonic() - s._t0


def test_report_has_timing_screen_and_tail(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    _feed(s, "\x1b[2Jhello world")
    with pytest.raises(AssertionError) as err:
        s.wait_for("nothing", timeout=0.2)
    msg = str(err.value)
    assert "timed out waiting for 'nothing'" in msg
    assert re.search(r"waited 0\.\d+s; session age \d+\.\ds; last PTY output at \d", msg), msg
    assert "1 chunks; eof=False" in msg
    assert "--- screen ---" in msg and "hello world" in msg
    assert "\\e[2Jhello world" in msg  # escape sequences are made visible
    written = list((tmp_path / "artifacts").glob("wait_for_failure_*.txt"))
    assert len(written) == 1
    assert written[0].read_text(encoding="utf-8") == msg


def test_report_before_any_output(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    with pytest.raises(AssertionError) as err:
        s.wait_for("x", timeout=0.1)
    assert "last PTY output at none" in str(err.value)
    assert "0 chunks" in str(err.value)


def test_match_still_returns_the_screen(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    _feed(s, "ready>")
    assert "ready>" in s.wait_for("ready>", timeout=1)
    assert not (tmp_path / "artifacts").exists()  # nothing is written on success


def test_tail_memory_is_bounded(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    for i in range(harness._TAIL_CHUNKS * 3):
        _feed(s, "x" * (harness._TAIL_CHUNK_CHARS * 2) + f" line {i}")
    assert len(s._tail) == harness._TAIL_CHUNKS
    assert all(len(c) <= harness._TAIL_CHUNK_CHARS for _t, c in s._tail)
    assert s._chunk_count == harness._TAIL_CHUNKS * 3  # the counter is exact, the tail is not
    with pytest.raises(AssertionError) as err:
        s.wait_for("never", timeout=0.1)
    tail = str(err.value).split("--- last PTY output", 1)[1]
    assert len(tail) <= harness._TAIL_CHARS + 200
    assert "line 599" in tail  # newest kept


def test_secrets_are_masked_even_when_split_across_chunks(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    half = len(FAKE_KEY) // 2
    _feed(s, "typing " + FAKE_KEY[:half])
    _feed(
        s, FAKE_KEY[half:] + " done gsk_abcdefghijklmnopqrstuvwx AIzaSyA-fakefakefakefakefakefake"
    )
    _feed(s, "sk-short")
    with pytest.raises(AssertionError) as err:
        s.wait_for("never", timeout=0.1)
    msg = str(err.value)
    written = next((tmp_path / "artifacts").glob("wait_for_failure_*.txt")).read_text("utf-8")
    for text in (msg, written):
        assert FAKE_KEY not in text
        assert FAKE_KEY[:half] not in text and FAKE_KEY[half:] not in text
        assert "gsk_abcdef" not in text and "AIzaSyA" not in text and "sk-short" not in text
        assert "[REDACTED]" in text
        assert "\x00" not in text
    assert "done" in msg  # surrounding output survives
    assert FAKE_KEY in s.raw  # ``raw`` itself is untouched: the secret scans still rely on it


def test_a_cut_chunk_does_not_leave_half_a_key(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_TAIL_CHUNK_CHARS", 20)
    s = _session(monkeypatch, tmp_path)
    _feed(s, FAKE_KEY + " tail")  # the 20-char cut lands inside the key
    ((_t, kept),) = s._tail
    assert kept == " tail", kept


# ── reader / child state ────────────────────────────────────────────────────


def _run_pump(s: TermSession, pty: _FakePty) -> None:
    s._p = pty
    s._t = threading.Thread(target=s._pump)
    s._t.start()
    s._t.join(5)


def _fail(s: TermSession) -> str:
    with pytest.raises(AssertionError) as err:
        s.wait_for("nothing", timeout=0.2)
    return str(err.value)


def test_eof_while_the_child_is_still_alive(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    _run_pump(s, _FakePty(alive=True, exitstatus=None, reads=["hello", EOFError("Pty is closed")]))
    msg = _fail(s)
    assert s.eof and "eof=True" in msg
    assert "reader: ended at" in msg and "via read-raised (EOFError: Pty is closed)" in msg
    assert "thread stopped" in msg
    assert "child at reader end: alive=True exitstatus=None" in msg
    assert "child now: alive=True exitstatus=None" in msg
    assert "hello" in msg  # output captured before EOF is still reported


def test_eof_after_the_child_exited(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    _run_pump(s, _FakePty(alive=False, exitstatus=7, reads=["bye", ""]))  # empty read = EOF
    msg = _fail(s)
    assert "via empty-read" in msg
    assert "child at reader end: alive=False exitstatus=7" in msg


def test_posix_style_signal_status_is_reported(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    _run_pump(s, _FakePty(alive=False, exitstatus=None, signalstatus=9, reads=[OSError(5, "EIO")]))
    msg = _fail(s)
    assert "exitstatus=None signalstatus=9" in msg
    assert "OSError" in msg


def test_reader_exception_is_captured_without_changing_behaviour(monkeypatch, tmp_path):
    s = _session(monkeypatch, tmp_path)
    errors: list[threading.ExceptHookArgs] = []
    monkeypatch.setattr(threading, "excepthook", errors.append)
    _run_pump(s, _FakePty(alive=True, reads=[RuntimeError(f"boom {FAKE_KEY}")]))
    assert len(errors) == 1  # the exception still escapes the thread, as before
    assert s.eof is False  # ... and ``eof`` is still not set
    msg = _fail(s)
    assert "via exception (RuntimeError: boom" in msg
    assert FAKE_KEY not in msg and "[REDACTED]" in msg
    assert "reader: ended" in msg and "eof=False" in msg


def test_unsupported_exit_status_is_reported_as_unknown(monkeypatch, tmp_path):
    class NoStatus:  # no ``exitstatus`` / ``signalstatus`` at all
        def isalive(self):
            return False

    s = _session(monkeypatch, tmp_path)
    s._p = NoStatus()
    assert "alive=False exitstatus=unknown(unsupported)" in _fail(s)


def test_state_collection_failure_never_breaks_the_harness(monkeypatch, tmp_path):
    class Hostile:
        def isalive(self):
            raise OSError("handle closed")

        @property
        def exitstatus(self):
            raise ValueError("native call failed")

        def read(self, _size):
            return ""

    s = _session(monkeypatch, tmp_path)
    _run_pump(s, Hostile())  # state is collected on the reader thread: must not raise
    assert s.eof is True
    msg = _fail(s)
    assert "alive=unknown(OSError)" in msg and "exitstatus=unknown(ValueError)" in msg

    s2 = _session(monkeypatch, tmp_path)
    s2._p = None  # not even an object: the report still comes out
    s2._t = None
    msg2 = _fail(s2)
    assert "timed out waiting for 'nothing'" in msg2
    assert "reader/child state: unknown" in msg2
