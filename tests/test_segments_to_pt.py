"""Unit tests for velune.cli.rendering.segments_to_pt — the Rich Segment ->
prompt_toolkit style/text bridge that lets the fullscreen REPL render real
markdown + syntax-highlighted code inside a FormattedTextControl-based pane.
"""

from __future__ import annotations

from rich.color import Color
from rich.console import Console
from rich.style import Style
from rich.text import Text

from velune.cli.rendering.markdown import CustomMarkdown
from velune.cli.rendering.segments_to_pt import render_to_fragments, style_to_pt


def test_style_to_pt_none_style_is_empty():
    assert style_to_pt(None) == ""


def test_style_to_pt_plain_style_is_empty():
    assert style_to_pt(Style()) == ""


def test_style_to_pt_standard_color_uses_ansi_name_not_number():
    # .number, not .name, drives the mapping — Color.name can be a
    # placeholder like "color(1)" depending on how the Color was built.
    style = Style(color=Color.parse("red"))
    assert style_to_pt(style) == "ansired"


def test_style_to_pt_bright_standard_color():
    style = Style(color=Color.parse("bright_red"))
    assert style_to_pt(style) == "ansibrightred"


def test_style_to_pt_white_maps_to_ansigray_per_prompt_toolkit_naming():
    # prompt_toolkit's own FG_ANSI_COLORS calls SGR 37 "ansigray", not
    # "ansiwhite" — ansiwhite is reserved for the bright variant (SGR 97).
    style = Style(color=Color.parse("white"))
    assert style_to_pt(style) == "ansigray"


def test_style_to_pt_truecolor():
    style = Style(color=Color.parse("#ff5fa2"))
    assert style_to_pt(style) == "#ff5fa2"


def test_style_to_pt_bgcolor():
    style = Style(bgcolor=Color.parse("#272822"))
    assert style_to_pt(style) == "bg:#272822"


def test_style_to_pt_bold_italic_underline_strike_dim_reverse():
    style = Style(bold=True, italic=True, underline=True, strike=True, dim=True, reverse=True)
    parts = style_to_pt(style).split()
    for expected in ("bold", "italic", "underline", "strike", "dim", "reverse"):
        assert expected in parts


def test_style_to_pt_combines_color_and_attributes():
    style = Style(color=Color.parse("#ff5fa2"), bgcolor=Color.parse("#272822"), bold=True)
    result = style_to_pt(style)
    assert "#ff5fa2" in result
    assert "bg:#272822" in result
    assert "bold" in result


def test_render_to_fragments_plain_text_roundtrips():
    console = Console(force_terminal=True, color_system="truecolor")
    lines = render_to_fragments(console, Text("hello"), width=40)
    plain = "".join(t for line in lines for _s, t in line)
    assert "hello" in plain


def test_render_to_fragments_markdown_bold_gets_bold_style():
    console = Console(force_terminal=True, color_system="truecolor")
    lines = render_to_fragments(console, CustomMarkdown("plain **bold** text"), width=40)
    flat = [(s, t) for line in lines for (s, t) in line]
    assert any("bold" in s for s, _t in flat)


def test_render_to_fragments_code_block_gets_syntax_highlight_colors():
    console = Console(force_terminal=True, color_system="truecolor")
    md = CustomMarkdown("```python\ndef f(n):\n    return f(n - 1)\n```")
    lines = render_to_fragments(console, md, width=60)
    flat = [(s, t) for line in lines for (s, t) in line]
    # Monokai theme colors are explicit hex — at least one fragment should
    # carry a real color (not just plain/background-only styling).
    assert any(s.startswith("#") for s, _t in flat)


def test_render_to_fragments_skips_control_segments():
    console = Console(force_terminal=True, color_system="truecolor")
    lines = render_to_fragments(console, Text("no control chars here"), width=40)
    for line in lines:
        for _style, text in line:
            assert "\x1b" not in text
