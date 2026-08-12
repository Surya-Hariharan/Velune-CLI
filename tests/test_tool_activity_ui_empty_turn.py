"""Regression: a tool-loop turn that ends with zero streamed text content
must not leave the fullscreen UI displaying a stale "thinking" word as if it
were the final response.

Root cause traced end-to-end: FullscreenREPLUI.begin_assistant() seeds its
own `_stream_text` with a thinking-word placeholder ("Cooking…", "Mapping…",
…); only update_assistant() ever overwrites it. `_ToolActivityUI._finish_live`
previously skipped calling update_assistant() whenever *its own*,
separately-accumulated `_stream_text` was empty (no content_delta events
fired — e.g. a turn that produced no text at all) — so the fullscreen UI's
placeholder was never overwritten, and finish_assistant()'s final render then
displayed that leftover thinking word as though it were the actual response.
This is what a user watching the REPL sees as being permanently "stuck" on
"Cooking…" with 0 tokens, even though the turn had already completed.
"""

from __future__ import annotations

from types import SimpleNamespace

from velune.cli.handlers.tool_chat import _ToolActivityUI


class _FakeFullscreenUI:
    def __init__(self):
        self.update_calls: list[tuple[str, bool]] = []
        self.finish_calls = 0

    def update_assistant(self, text: str, *, final: bool = False) -> None:
        self.update_calls.append((text, final))

    def finish_assistant(self) -> None:
        self.finish_calls += 1


class _FakeContainer:
    def get(self, _name):
        raise RuntimeError("no workspace in this fake container")


def _fake_repl(fullscreen_ui):
    return SimpleNamespace(
        console=SimpleNamespace(print=lambda *a, **k: None),
        container=_FakeContainer(),
        _session_id="s1",
        _fullscreen_ui=fullscreen_ui,
    )


def test_empty_turn_still_overwrites_the_placeholder_text():
    fullscreen = _FakeFullscreenUI()
    ui = _ToolActivityUI(_fake_repl(fullscreen))
    assert ui._stream_text == ""  # no content_delta ever fired for this turn

    ui._finish_live()

    assert fullscreen.update_calls == [("", True)], (
        "update_assistant must be called even with empty text, so the "
        "fullscreen UI's thinking-word placeholder gets overwritten"
    )
    assert fullscreen.finish_calls == 1


def test_non_empty_turn_still_forwards_the_real_text():
    fullscreen = _FakeFullscreenUI()
    ui = _ToolActivityUI(_fake_repl(fullscreen))
    ui._stream_text = "Hello! How can I help?"

    ui._finish_live()

    assert fullscreen.update_calls == [("Hello! How can I help?", True)]
    assert fullscreen.finish_calls == 1


def test_stream_text_is_reset_after_finishing():
    fullscreen = _FakeFullscreenUI()
    ui = _ToolActivityUI(_fake_repl(fullscreen))
    ui._stream_text = "some text"

    ui._finish_live()

    assert ui._stream_text == ""
