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
    s.eof = False
    return s


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
