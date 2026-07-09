"""Bridge Rich `Segment` streams into prompt_toolkit style/text fragments.

Renders a Rich renderable (markdown, syntax-highlighted code, panels) inside
a `prompt_toolkit.layout.controls.FormattedTextControl`-based pane instead of
a real terminal — the fullscreen REPL's transcript is exactly such a pane, so
this is how streamed assistant responses get real markdown + syntax
highlighting instead of flat unstyled text.

Walks `Console.render_lines`' `Segment` stream directly (Rich's own
layout/word-wrap engine, already width-constrained) rather than round-
tripping through ANSI escape text: cheaper (no serialize-then-reparse), and
immune to the non-SGR CSI sequences `prompt_toolkit.formatted_text.ANSI`
doesn't fully handle, since we never serialize to escape codes at all.
"""

from __future__ import annotations

from rich.color import Color, ColorType
from rich.console import Console, ConsoleRenderable
from rich.style import Style as RichStyle

# Index = the classic ANSI 16-color number (Color.number for ColorType.STANDARD).
_STANDARD_ANSI_NAMES = (
    "ansiblack",
    "ansired",
    "ansigreen",
    "ansiyellow",
    "ansiblue",
    "ansimagenta",
    "ansicyan",
    "ansigray",
    "ansibrightblack",
    "ansibrightred",
    "ansibrightgreen",
    "ansibrightyellow",
    "ansibrightblue",
    "ansibrightmagenta",
    "ansibrightcyan",
    "ansiwhite",
)


def _color_to_pt(color: Color) -> str:
    """Map a Rich Color to a prompt_toolkit color name/hex.

    Standard 16-color entries map to prompt_toolkit's own `ansi*` names (so
    they keep resolving against the terminal's own theme) using `.number`,
    not `.name` — `.name` is unreliable (e.g. `Color.from_ansi(1).name` is
    the placeholder `"color(1)"`, not `"red"`, depending on how the Color was
    constructed). Everything else (256-color, truecolor) is converted to an
    explicit `#rrggbb` hex via `get_truecolor()`.
    """
    if color.type is ColorType.STANDARD and color.number is not None and 0 <= color.number < 16:
        return _STANDARD_ANSI_NAMES[color.number]
    triplet = color.get_truecolor()
    return f"#{triplet.red:02x}{triplet.green:02x}{triplet.blue:02x}"


def style_to_pt(style: RichStyle | None) -> str:
    """Map a `rich.style.Style` to a prompt_toolkit style string."""
    if style is None:
        return ""
    parts: list[str] = []
    if style.color is not None and style.color.type is not ColorType.DEFAULT:
        parts.append(_color_to_pt(style.color))
    if style.bgcolor is not None and style.bgcolor.type is not ColorType.DEFAULT:
        parts.append("bg:" + _color_to_pt(style.bgcolor))
    if style.bold:
        parts.append("bold")
    if style.dim:
        parts.append("dim")
    if style.italic:
        parts.append("italic")
    if style.underline:
        parts.append("underline")
    if style.strike:
        parts.append("strike")
    if style.reverse:
        parts.append("reverse")
    return " ".join(parts)


def render_to_fragments(
    console: Console,
    renderable: ConsoleRenderable,
    width: int,
) -> list[list[tuple[str, str]]]:
    """Render *renderable* to prompt_toolkit style/text fragments, one list per line."""
    options = console.options.update(width=max(1, width))
    segment_lines = console.render_lines(renderable, options, pad=False, new_lines=False)
    return [
        [(style_to_pt(seg.style), seg.text) for seg in segments if not seg.is_control]
        for segments in segment_lines
    ]
