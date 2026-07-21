"""Tests for velune.cli.fullscreen — the persistent full-screen REPL UI.

Covers the three Stage-1 fixes: color-preserving console output (instead of
_ConsoleSink stripping all ANSI), the command palette actually being wired
into the layout's Float list, and streaming responses rendering through real
markdown + syntax highlighting instead of flat text.
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import DummyOutput

from velune.cli.command_palette import CommandPalette
from velune.cli.fullscreen import FullscreenREPLUI, _strip_non_sgr_csi
from velune.cli.slash_commands import SlashCommand


def _make_ui(*, command_palette=None) -> FullscreenREPLUI:
    return FullscreenREPLUI(
        status_state=None,
        history=InMemoryHistory(),
        completer=None,
        validator=None,
        style_fragments={},
        key_bindings=KeyBindings(),
        on_interrupt=lambda e: True,
        command_palette=command_palette,
        output=DummyOutput(),
    )


# ── SGR-preserving console sink ─────────────────────────────────────────────


def test_strip_non_sgr_csi_preserves_complete_sgr_sequences():
    # Regression test: a naive "match up to the first non-'m' char" pattern
    # backtracks into the middle of a legitimate SGR sequence and leaves a
    # stray 'm' behind. The real implementation must keep SGR fully intact.
    data = "\x1b[1;31mcolored line\x1b[0m"
    assert _strip_non_sgr_csi(data) == data


def test_strip_non_sgr_csi_removes_cursor_and_erase_sequences():
    data = "\x1b[?25lspinner\x1b[?25h done \x1b[32mgreen\x1b[0m"
    result = _strip_non_sgr_csi(data)
    assert "\x1b[?25l" not in result
    assert "\x1b[?25h" not in result
    assert "\x1b[32m" in result
    assert "\x1b[0m" in result
    assert "spinner" in result and "green" in result


def test_strip_non_sgr_csi_no_stray_characters_leak():
    data = "\x1b[1;31mtext\x1b[0m"
    result = _strip_non_sgr_csi(data)
    # The bug this guards against: a lone 'm' left over from a truncated match.
    assert not result.startswith("m")
    assert "textm" not in result


def test_console_print_preserves_color_through_sink():
    ui = _make_ui()
    ui.console.print("[bold red]colored line[/bold red]")
    line = ui._lines[-1]
    assert line.text == "colored line"
    assert line.fragments is not None
    styles = {s for s, _t in line.fragments}
    assert any("ansired" in s and "bold" in s for s in styles)


def test_console_color_system_is_truecolor():
    ui = _make_ui()
    assert ui.console.color_system == "truecolor"


# ── Command palette wiring ──────────────────────────────────────────────────


def _sample_commands() -> list[SlashCommand]:
    async def _handler(args: str) -> None:
        return None

    return [
        SlashCommand(
            name="help",
            aliases=["h"],
            description="Show all available commands",
            usage="/help",
            handler=_handler,
            category="System",
        )
    ]


def test_no_palette_means_one_float_unchanged():
    ui = _make_ui(command_palette=None)
    # The root is a width-capping VSplit (see `_MAX_CONTENT_WIDTH` in
    # fullscreen.py) wrapping the actual FloatContainer as its one child.
    content = ui._app.layout.container.children[0]
    assert len(content.floats) == 1


def test_palette_float_is_composed_into_layout():
    palette = CommandPalette(_sample_commands())
    ui = _make_ui(command_palette=palette)
    content = ui._app.layout.container.children[0]

    assert len(content.floats) == 2
    # This is the exact bug that was fixed: the palette's ConditionalContainer
    # (self-gated on is_active()) previously was never added to any Float
    # actually rendered by FullscreenREPLUI — .attach() only worked against
    # a PromptSession, which the fullscreen app doesn't use.
    second = content.floats[1].content
    assert type(second).__name__ == "ConditionalContainer"


# ── Prompt box border rendering ─────────────────────────────────────────────


class _FakeSize:
    def __init__(self, columns: int, rows: int = 24) -> None:
        self.columns = columns
        self.rows = rows


def _fragments_text(fragments) -> str:
    return "".join(t for _s, t in fragments)


def test_prompt_top_border_matches_terminal_width():
    ui = _make_ui()
    ui._app.output.get_size = lambda: _FakeSize(50)
    text = _fragments_text(ui._render_prompt_top_border())
    assert len(text) == 50
    assert text.startswith("╭")
    assert text.endswith("╮")


def test_prompt_bottom_border_embeds_hint_and_matches_width():
    ui = _make_ui()
    ui._app.output.get_size = lambda: _FakeSize(70)
    text = _fragments_text(ui._render_prompt_bottom_border())
    assert len(text) == 70
    assert text.startswith("╰")
    assert text.endswith("╯")
    assert "Enter send" in text


def test_prompt_bottom_border_drops_hint_when_too_narrow():
    ui = _make_ui()
    ui._app.output.get_size = lambda: _FakeSize(20)
    text = _fragments_text(ui._render_prompt_bottom_border())
    assert len(text) == 20
    assert "Enter send" not in text
    assert text == "╰" + "─" * 18 + "╯"


def test_prompt_line_prefix_first_line_has_arrow_glyph():
    ui = _make_ui()
    text = _fragments_text(ui._prompt_line_prefix(0, 0))
    assert "❯" in text


def test_prompt_line_prefix_continuation_line_has_no_arrow_glyph():
    ui = _make_ui()
    text = _fragments_text(ui._prompt_line_prefix(1, 0))
    assert "❯" not in text


# ── Streaming through real markdown ──────────────────────────────────────────


async def _stream_markdown_text(ui: FullscreenREPLUI, text: str) -> None:
    ui.begin_assistant()
    ui.update_assistant(text, final=True)
    ui.finish_assistant()


def test_streamed_response_renders_markdown_and_code_highlighting():
    ui = _make_ui()
    md_text = "Recursion calls **itself**.\n\n```python\ndef f(n):\n    return f(n - 1)\n```\n"
    asyncio.run(_stream_markdown_text(ui, md_text))

    rendered_lines = [line for line in ui._lines if line.fragments]
    assert rendered_lines, "expected at least one fragment-carrying (rendered) line"

    all_text = "\n".join(line.text for line in ui._lines)
    assert "```" not in all_text  # fences are parsed away, not shown literally
    assert "def f(n):" in all_text

    all_styles = {s for line in rendered_lines for s, _t in line.fragments}
    assert any("bold" in s for s in all_styles)  # from **itself**
    assert any(s.startswith("#") for s in all_styles)  # syntax-highlight color


def test_streaming_falls_back_to_flat_text_on_markdown_render_failure(monkeypatch):
    import velune.cli.fullscreen as fullscreen_mod

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated markdown render failure")

    monkeypatch.setattr(fullscreen_mod, "render_to_fragments", _boom)

    ui = _make_ui()
    asyncio.run(_stream_markdown_text(ui, "plain text that should still show up"))

    all_text = "\n".join(line.text for line in ui._lines)
    assert "plain text that should still show up" in all_text
