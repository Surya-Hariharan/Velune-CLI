"""OSC 52 clipboard write helpers used by the REPL's Ctrl+Y / Ctrl+G actions."""

from __future__ import annotations

import base64
from types import SimpleNamespace

from velune.cli.clipboard import (
    build_osc52_sequence,
    copy_to_clipboard,
    extract_last_code_block,
)


def test_build_osc52_sequence_wraps_base64_payload():
    seq = build_osc52_sequence("hello")
    encoded = base64.b64encode(b"hello").decode("ascii")
    assert seq == f"\x1b]52;c;{encoded}\x07"


def test_build_osc52_sequence_wraps_for_tmux(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
    seq = build_osc52_sequence("hi")
    assert seq.startswith("\x1bPtmux;")
    assert seq.endswith("\x1b\\")


def test_build_osc52_sequence_truncates_huge_payload():
    seq = build_osc52_sequence("x" * 500_000)
    # Base64 of a truncated 100_000-byte payload is well under the full
    # 500_000-char input encoded, so this is a cheap sanity bound rather
    # than an exact-length assertion (base64 padding varies).
    assert len(seq) < 200_000


class _FakeOutput:
    def __init__(self, raise_on_write: bool = False) -> None:
        self.written: list[str] = []
        self.flushed = False
        self._raise = raise_on_write

    def write_raw(self, data: str) -> None:
        if self._raise:
            raise OSError("no tty")
        self.written.append(data)

    def flush(self) -> None:
        self.flushed = True


def test_copy_to_clipboard_writes_through_app_output():
    output = _FakeOutput()
    app = SimpleNamespace(output=output)
    assert copy_to_clipboard(app, "hello") is True
    assert output.written and "hello" not in output.written[0]  # base64, not raw
    assert output.flushed is True


def test_copy_to_clipboard_false_on_empty_text_or_missing_app():
    assert copy_to_clipboard(None, "hello") is False
    assert copy_to_clipboard(SimpleNamespace(output=_FakeOutput()), "") is False
    assert copy_to_clipboard(SimpleNamespace(output=None), "hello") is False


def test_copy_to_clipboard_swallows_write_errors():
    app = SimpleNamespace(output=_FakeOutput(raise_on_write=True))
    assert copy_to_clipboard(app, "hello") is False


def test_extract_last_code_block_returns_last_fence_content():
    text = "some text\n```python\nprint(1)\n```\nmore\n```js\nconsole.log(2)\n```\n"
    assert extract_last_code_block(text) == "console.log(2)"


def test_extract_last_code_block_none_when_no_fence():
    assert extract_last_code_block("no code here") is None
    assert extract_last_code_block("") is None


def test_extract_last_code_block_handles_unterminated_fence():
    text = "```python\nprint('unterminated')"
    assert extract_last_code_block(text) == "print('unterminated')"
