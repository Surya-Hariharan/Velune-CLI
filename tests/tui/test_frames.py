"""Unit tests for the frame comparator (no PTY, no CLI): fast and always run."""

from __future__ import annotations

import json
import re
import threading

import pyte
import pytest

from . import frames


class _Fake:
    """Minimal stand-in for TermSession: a pyte screen plus the lock."""

    def __init__(self, rows: int = 6, cols: int = 40) -> None:
        self.screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self.screen)
        self._lock = threading.Lock()

    def feed(self, data: str) -> _Fake:
        self._stream.feed(data)
        return self


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(frames, "BASELINE_DIR", tmp_path / "baselines")
    monkeypatch.setattr(frames, "ARTIFACT_DIR", tmp_path / "artifacts")
    monkeypatch.delenv(frames.UPDATE_ENV, raising=False)
    return tmp_path


def _approve(name: str, fake: _Fake, monkeypatch, **kw) -> None:
    monkeypatch.setenv(frames.UPDATE_ENV, "1")
    frames.assert_frame(fake, name, **kw)
    monkeypatch.delenv(frames.UPDATE_ENV)


def test_identical_frame_passes(isolated, monkeypatch):
    f = _Fake().feed("\x1b[31mhello\x1b[0m world\r\nsecond")
    _approve("a", f, monkeypatch)
    frames.assert_frame(f, "a")


def test_text_change_fails_and_names_row_and_columns(isolated, monkeypatch):
    _approve("a", _Fake().feed("hello world"), monkeypatch)
    with pytest.raises(AssertionError) as e:
        frames.assert_frame(_Fake().feed("hello wurld"), "a")
    msg = str(e.value)
    assert "row 0: text differs" in msg
    assert "col 7: 'o' -> 'u'" in msg
    assert (
        isolated / "artifacts" / "a.actual.json"
    ).exists()  # failure dump is outside the repo tree


def test_style_only_change_fails_with_columns_and_attributes(isolated, monkeypatch):
    _approve("a", _Fake().feed("\x1b[31mred\x1b[0m plain"), monkeypatch)
    with pytest.raises(AssertionError) as e:
        frames.assert_frame(_Fake().feed("\x1b[32mred\x1b[0m plain"), "a")
    msg = str(e.value)
    assert "row 0: style differs" in msg
    assert "cols 0-2: style" in msg and "red/default" in msg and "green/default" in msg


def test_bold_attribute_change_is_detected(isolated, monkeypatch):
    _approve("a", _Fake().feed("abc"), monkeypatch)
    with pytest.raises(AssertionError, match="style differs"):
        frames.assert_frame(_Fake().feed("\x1b[1mabc"), "a")


def test_cursor_move_fails_unless_ignored(isolated, monkeypatch):
    _approve("a", _Fake().feed("abc"), monkeypatch)
    moved = _Fake().feed("abc\x1b[2;5H")
    with pytest.raises(AssertionError, match="cursor: expected"):
        frames.assert_frame(moved, "a")
    frames.assert_frame(moved, "a", cursor=False)


def test_cursor_visibility_is_part_of_the_frame(isolated, monkeypatch):
    _approve("a", _Fake().feed("abc"), monkeypatch)
    with pytest.raises(AssertionError, match="hidden=True"):
        frames.assert_frame(_Fake().feed("abc\x1b[?25l"), "a")


def test_size_change_is_reported(isolated, monkeypatch):
    _approve("a", _Fake(6, 40).feed("x"), monkeypatch)
    with pytest.raises(AssertionError, match="size: expected 6x40, got 8x50"):
        frames.assert_frame(_Fake(8, 50).feed("x"), "a")


def test_missing_baseline_fails_with_instructions(isolated):
    with pytest.raises(AssertionError, match="no approved baseline") as e:
        frames.assert_frame(_Fake().feed("x"), "never_recorded")
    assert frames.UPDATE_ENV in str(e.value)
    assert not (isolated / "baselines").exists(), "a missing baseline must never be auto-created"


def test_dynamic_values_are_masked_so_runs_compare_equal(isolated, monkeypatch):
    a = _Fake(3, 80).feed(
        "v0.9.8  ~/AppData/Local/Temp/tmpabc123/w  not indexed\r\n3 tokens · ~$0.0001\r\n12/128k 1.4s"
    )
    b = _Fake(3, 80).feed(
        "v1.0.0  C:\\Users\\x\\Temp\\tmpzzz999\\w  not indexed\r\n19 tokens · ~$0.0042\r\n99/128k 22s"
    )
    _approve("dyn", a, monkeypatch)
    # The cursor trails dynamic text of different lengths, so it is not compared here.
    frames.assert_frame(b, "dyn", cursor=False)


def test_masking_does_not_hide_real_text_changes(isolated, monkeypatch):
    _approve("dyn", _Fake(2, 60).feed("v0.9.8 ready  not indexed"), monkeypatch)
    with pytest.raises(AssertionError, match="row 0: text differs"):
        frames.assert_frame(_Fake(2, 60).feed("v0.9.8 ready  INDEXED"), "dyn")


def test_masked_rows_skip_style_but_unmasked_rows_do_not(isolated, monkeypatch):
    _approve("m", _Fake(3, 40).feed("v1.0.0\r\nplain"), monkeypatch)
    # Style change on the masked (version) row: tolerated. On the plain row: caught.
    frames.assert_frame(_Fake(3, 40).feed("\x1b[31mv1.0.1\x1b[0m\r\nplain"), "m")
    with pytest.raises(AssertionError, match="row 1: style differs"):
        frames.assert_frame(_Fake(3, 40).feed("v1.0.1\r\n\x1b[31mplain"), "m")


def test_extra_masks_apply(isolated, monkeypatch):
    mask = (re.compile(r"\d+"),)
    _approve("x", _Fake(2, 30).feed("job 123 done"), monkeypatch, extra_masks=mask)
    frames.assert_frame(_Fake(2, 30).feed("job 987 done"), "x", extra_masks=mask)


def test_key_shaped_strings_never_reach_a_baseline(isolated, monkeypatch):
    secret = "sk-velune-test-0000000000000000FAKE"
    _approve("k", _Fake(2, 60).feed(f"key: {secret}"), monkeypatch)
    assert secret not in (isolated / "baselines" / "k.json").read_text(encoding="utf-8")


def test_baseline_file_is_stable_and_diff_friendly(isolated, monkeypatch):
    f = _Fake(3, 20).feed("one\r\ntwo")
    _approve("s", f, monkeypatch)
    first = (isolated / "baselines" / "s.json").read_text(encoding="utf-8")
    _approve("s", f, monkeypatch)
    assert first == (isolated / "baselines" / "s.json").read_text(encoding="utf-8")
    assert "\r" not in first
    json.loads(first)  # valid JSON
    assert first.count("\n") > 6  # one row per line, not a single blob


def test_shipped_baselines_are_sanitized():
    """Every committed fixture: valid, no secrets, no machine-specific paths."""
    secret_re = re.compile(r"(sk-|gsk_|AIza)[A-Za-z0-9_\-]{12,}")
    path_re = re.compile(r"(?i)(users[\\/]|appdata|[\\/]tmp[\\/]|C:\\)")
    for p in sorted(frames.BASELINE_DIR.glob("*.json")) if frames.BASELINE_DIR.exists() else []:
        text = p.read_text(encoding="utf-8")
        json.loads(text)
        assert not secret_re.search(text), p
        assert not path_re.search(text), f"machine-specific path in {p}"
