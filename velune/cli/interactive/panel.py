"""Shared "command palette" panel chrome for standalone interactive widgets.

The REPL's floating command palette (``velune.cli.command_palette``) is the
surface users learn the app through: a framed, surfaced panel with a search
column on the left and a detail column on the right. Every other picker in the
app — choosing a provider to connect, entering its API key — used a bare
left-aligned list on the default background, so the same act of "pick a thing"
looked like two different products depending on which command you reached it
from.

This module holds the chrome those widgets share with the palette so the look
is defined once. It deliberately does *not* import ``command_palette``: that
module renders through ``class:palette.*`` style names registered on the REPL's
long-lived Application, and standalone widgets run in their own throwaway
Application with no style registry. The colors therefore come straight from
``design`` as inline fragment styles, which resolve anywhere.
"""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.layout.containers import AnyContainer, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame

from velune.cli import design

# Panel background. Applied to the Frame and to every Window inside it so that
# fragments carrying only a foreground color inherit the surfaced background.
PANEL_STYLE = f"bg:{design.SURFACE} fg:{design.FAINT}"

# Fragment styles, mirroring the palette.* style names one-for-one.
TITLE = f"bg:{design.SURFACE} fg:{design.WHITE} bold"
LABEL = f"bg:{design.SURFACE} fg:{design.MUTED} bold"
QUERY = f"bg:{design.SURFACE} fg:{design.ACCENT} bold"
GROUP = f"bg:{design.SURFACE} fg:{design.MUTED} bold"
ROW = f"bg:{design.SURFACE} fg:{design.WHITE}"
SELECTED = f"bg:{design.LIGHT_BG} fg:{design.ACCENT_SOFT} bold"
TEXT = f"bg:{design.SURFACE} fg:{design.WHITE}"
CODE = f"bg:{design.SURFACE} fg:{design.ACCENT_SOFT}"
MUTED = f"bg:{design.SURFACE} fg:{design.FAINT}"
WARNING = f"bg:{design.SURFACE} fg:{design.WARN}"


def framed(body: AnyContainer, title: str | Callable[[], str]) -> Frame:
    """Wrap *body* in the palette's titled, surfaced frame.

    *title* may be a callable, for hosts whose caption changes while the frame
    stays on screen — ``InlineFlow`` re-titles one long-lived frame as the user
    moves between steps. A plain string is read once, at construction.
    """

    def _caption() -> StyleAndTextTuples:
        text = title() if callable(title) else title
        return [(f"bg:{design.SURFACE} fg:{design.ACCENT} bold", f" {text.upper()} ")]

    return Frame(body, title=_caption, style=PANEL_STYLE)


def two_pane(
    left: Callable[[], StyleAndTextTuples],
    right: Callable[[], StyleAndTextTuples],
) -> VSplit:
    """Search column | divider | detail column, at the palette's proportions."""
    left_window = Window(
        content=FormattedTextControl(left),
        width=Dimension(min=25, preferred=34),
        dont_extend_width=False,
        style=PANEL_STYLE,
    )
    divider = Window(width=1, char="|", style=PANEL_STYLE)
    right_window = Window(
        content=FormattedTextControl(right),
        width=Dimension(min=30, weight=2),
        style=PANEL_STYLE,
    )
    return VSplit([left_window, divider, right_window], padding=1, padding_style=PANEL_STYLE)


def window_start(total: int, selected: int, limit: int) -> int:
    """First visible row index so *selected* stays centered in a *limit* window."""
    if total <= limit:
        return 0
    return min(max(0, selected - limit // 2), total - limit)
