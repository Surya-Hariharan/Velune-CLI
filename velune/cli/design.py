"""Central design tokens for the Velune CLI.

Every colour constant below is a *role* ("the accent", "the panel background"),
and every one of them is supplied by the active :class:`~velune.cli.themes.Theme`.
The module-level names are the stable contract the rest of the CLI imports;
:func:`apply_theme` repoints them at a different theme's values.

Reading the tokens
------------------
Call sites must read these as module attributes (``design.ACCENT``), never as
``from velune.cli.design import ACCENT`` — a from-import copies the value at
import time and would freeze on whichever theme happened to be active then.
The same rule already governed ``OK``/``WARN``/``DANGER`` under colourblind
mode; theming simply widens it to the whole palette.

Style *dicts* built from these tokens have the same hazard one level up, which
is why the palette/status-bar modules expose ``*_styles()`` builder functions
rather than module-level dicts — see ``velune.cli.command_palette``.

Nothing here probes the terminal at import time; :func:`color_enabled` is
evaluated lazily so the palette degrades gracefully under ``NO_COLOR`` and on
dumb/unsupported terminals without affecting deterministic rendering.
"""

from __future__ import annotations

import os
import sys

from velune.cli import themes

#: The theme currently painting the UI. Swap it with :func:`apply_theme`.
_active_theme: themes.Theme = themes.default_theme()

# --- Brand palette ----------------------------------------------------------
# One vivid hue drives the whole theme — the logo wordmark, prompt glyph,
# headings, and active/selected state — set against neutrals so the accent
# reads as energetic without turning the UI into noise.
ACCENT = _active_theme.primary  # wordmark, primary brand, prompt prefix
ACCENT_SOFT = _active_theme.secondary  # secondary elements, arrows

# Companion accent, a distinct hue from ACCENT. Used where a surface must read
# as a *sibling* of an ACCENT one rather than the same thing — the model
# palette sits in exactly the geometry the command palette just vacated, so a
# distinct hue is what tells you the list under your cursor changed meaning
# (commands → models). Named CYAN for historical reasons; under a non-default
# theme it is whatever companion hue that theme defines.
CYAN = _active_theme.accent
CYAN_SOFT = _active_theme.accent_soft

# The brand wordmark is painted as a horizontal gradient across these three
# stops. `gradient_hex(t)` interpolates between them for any t in [0, 1];
# other surfaces can reuse it for progress fills, meters, etc.
GRAD_START, GRAD_MID, GRAD_END = _active_theme.gradient

# Reuses of the accent — kept as separate names because other modules
# reference them by role, not because they carry a distinct hue.
PRIMARY_GREEN = ACCENT_SOFT  # (emphasis, highlights)
GREEN = _active_theme.success  # = OK (accents, active states, success)

HIGHLIGHT = ACCENT  # (modes, indicators)
ENERGY = ACCENT_SOFT  # (active processes)

# Info & feedback — desaturated, accent-tinted gray rather than a new hue.
INFO = _active_theme.info  # informational text
SUBTLE = _active_theme.muted  # subtle elements

# Semantic state colors (shared by status bar, badges, diffs). Desaturated so
# they sit quietly against the theme's background while staying legible.
OK = _active_theme.success
WARN = _active_theme.warning
DANGER = _active_theme.error

# Neutrals.
BACKGROUND = _active_theme.background  # fullscreen REPL background
WHITE = _active_theme.foreground  # primary body text (soft off-white)
SECONDARY = _active_theme.foreground_dim  # neutral secondary text
MUTED = _active_theme.muted  # secondary/dim text
FAINT = _active_theme.border  # frame glyphs, separators
SURFACE = _active_theme.panel  # panel background
LIGHT_BG = _active_theme.selection  # selected rows, slightly lighter panels

# --- Semantic role aliases -------------------------------------------------
# NOTE: "PINK" is a legacy name from a previous brand palette — it now points
# at the theme accent, not an actual pink hue. Left unrenamed to avoid a mass
# rename across every importer.
PINK = ACCENT
SUCCESS = OK
ERROR = DANGER
ACCENT_TEXT = ACCENT
CONTROL = ACCENT  # orchestration/control
PRIVACY = PRIMARY_GREEN  # local-first, secure
SPEED = HIGHLIGHT  # performance, energy


def apply_theme(theme: themes.Theme) -> None:
    """Repoint every colour token at *theme*.

    Mutates the module globals in place rather than returning a new palette,
    because ~40 modules already reference these by attribute — the same
    mechanism ``set_colorblind_mode`` has always used, widened to the full
    token set. Anything rendered after this call picks up the new colours.

    Colourblind mode is reapplied last: it is an accessibility modifier that
    outranks the theme's own severity trio, so switching themes while it is on
    must not quietly restore theme colours the user turned off.
    """
    global _active_theme
    global ACCENT, ACCENT_SOFT, CYAN, CYAN_SOFT
    global GRAD_START, GRAD_MID, GRAD_END
    global PRIMARY_GREEN, GREEN, HIGHLIGHT, ENERGY, INFO, SUBTLE
    global OK, WARN, DANGER
    global BACKGROUND, WHITE, SECONDARY, MUTED, FAINT, SURFACE, LIGHT_BG
    global PINK, SUCCESS, ERROR, ACCENT_TEXT, CONTROL, PRIVACY, SPEED

    _active_theme = theme

    ACCENT = theme.primary
    ACCENT_SOFT = theme.secondary
    CYAN = theme.accent
    CYAN_SOFT = theme.accent_soft
    GRAD_START, GRAD_MID, GRAD_END = theme.gradient

    PRIMARY_GREEN = ACCENT_SOFT
    GREEN = theme.success
    HIGHLIGHT = ACCENT
    ENERGY = ACCENT_SOFT
    INFO = theme.info
    SUBTLE = theme.muted

    OK, WARN, DANGER = theme.success, theme.warning, theme.error

    BACKGROUND = theme.background
    WHITE = theme.foreground
    SECONDARY = theme.foreground_dim
    MUTED = theme.muted
    FAINT = theme.border
    SURFACE = theme.panel
    LIGHT_BG = theme.selection

    PINK = ACCENT
    ACCENT_TEXT = ACCENT
    CONTROL = ACCENT
    PRIVACY = PRIMARY_GREEN
    SPEED = HIGHLIGHT

    # Re-derives SUCCESS/ERROR and honours an active colourblind override.
    set_colorblind_mode(_colorblind_mode)


def active_theme() -> themes.Theme:
    """The theme currently painting the UI."""
    return _active_theme


def active_theme_id() -> str:
    """Id of the theme currently painting the UI."""
    return _active_theme.id


# --- Colorblind-safe alternate severity palette -----------------------------
# Okabe-Ito palette (https://jfly.uni-koeln.de/color/) — chosen because its
# hues stay distinguishable under deuteranopia, protanopia, and tritanopia,
# the three common forms of color vision deficiency, unlike the default
# amber/sage/brick-red trio which leans on a red-green contrast that two of
# those three conditions collapse.
#
# `set_colorblind_mode()` reassigns the OK/WARN/DANGER/SUCCESS/ERROR module
# globals in place. Every call site in the codebase reads these through
# `design.OK` etc. (attribute access on the module, not a `from ... import`
# copy), so the swap takes effect immediately for anything rendered after the
# call — no cache to invalidate.
_COLORBLIND_SEVERITY = {
    "OK": "#009e73",  # bluish green
    "WARN": "#e69f00",  # orange
    "DANGER": "#d55e00",  # vermillion
}

_colorblind_mode = False


def set_colorblind_mode(enabled: bool) -> None:
    """Switch OK/WARN/DANGER (and their SUCCESS/ERROR aliases) between the
    active theme's severity trio and the colorblind-safe alternate above.

    "Off" restores the trio from :func:`active_theme`, not from a snapshot
    taken at import — otherwise toggling colourblind mode off after a theme
    change would silently reinstate the *previous* theme's severity colours.
    """
    global _colorblind_mode, OK, WARN, DANGER, SUCCESS, ERROR
    _colorblind_mode = bool(enabled)
    if _colorblind_mode:
        OK, WARN, DANGER = (
            _COLORBLIND_SEVERITY["OK"],
            _COLORBLIND_SEVERITY["WARN"],
            _COLORBLIND_SEVERITY["DANGER"],
        )
    else:
        OK, WARN, DANGER = (
            _active_theme.success,
            _active_theme.warning,
            _active_theme.error,
        )
    SUCCESS, ERROR = OK, DANGER


def is_colorblind_mode() -> bool:
    """Return whether the colorblind-safe severity palette is active."""
    return _colorblind_mode


# --- Reduced motion ----------------------------------------------------------
_reduced_motion = False


def set_reduced_motion(enabled: bool) -> None:
    """Enable/disable reduced-motion mode.

    When enabled, the fullscreen REPL's thinking/tool-card spinners render a
    single static frame instead of animating — no cycling glyph, no cycling
    verb text, no periodic `invalidate()` calls from an animation task.
    """
    global _reduced_motion
    _reduced_motion = bool(enabled)


def reduced_motion_enabled() -> bool:
    """Return True when animated UI elements (spinners) should stay static.

    Checks the explicit toggle set via `set_reduced_motion()` first (config-
    driven, persisted via `/theme motion off`), then falls back to the
    ``VELUNE_REDUCED_MOTION`` environment variable — the same opt-in
    convention ``NO_COLOR`` uses for `color_enabled()` above — so a terminal
    or OS-level "prefers reduced motion" preference can be honored without
    touching velune.toml.
    """
    if _reduced_motion:
        return True
    val = os.environ.get("VELUNE_REDUCED_MOTION", "").strip().lower()
    return val not in ("", "0", "false", "no")


# --- Icons (semantic glyphs) ----------------------------------------------
# Single-width chars guaranteed to render in any modern terminal.
ICON_SUCCESS = "✓"
ICON_ERROR = "✗"
ICON_WARNING = "⚠"
ICON_INFO = "·"
ICON_ARROW = "→"
ICON_SELECTED = "▶"
ICON_UNSELECTED = " "
ICON_BULLET = "•"
ICON_ELLIPSIS = "…"
ICON_CURSOR = "█"
ICON_DIAMOND = "◆"  # brand mark / compact logo lockup
ICON_BRANCH = "⎇"  # git branch indicator

# --- Spacing tokens --------------------------------------------------------
# Rich padding tuples: (top/bottom, left/right)
PADDING_NONE = (0, 0)
PADDING_COMPACT = (0, 1)  # tight inline use
PADDING_DEFAULT = (0, 2)  # standard panels
PADDING_RELAXED = (1, 2)  # modals, dialogs

# --- Separator glyph -------------------------------------------------------
SEP = "  ·  "  # metadata separator used in status bar and key hints

# --- Context-pressure thresholds ------------------------------------------
# Percent of context window consumed. Shared by the prompt badge, bottom
# status bar, and /context command so all three agree on thresholds.
CTX_WARN_PCT = 70.0
CTX_DANGER_PCT = 90.0


def context_state(pct: float) -> str:
    """Map a context-usage percentage to a semantic state name (ok/warn/danger)."""
    if pct < CTX_WARN_PCT:
        return "ok"
    if pct < CTX_DANGER_PCT:
        return "warn"
    return "danger"


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c))) for c in rgb))


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def gradient_hex(t: float) -> str:
    """Interpolate the brand gradient (violet → blue → teal) at position *t*.

    ``t`` is clamped to ``[0, 1]``. The first half blends GRAD_START→GRAD_MID,
    the second half GRAD_MID→GRAD_END, so the midpoint lands exactly on the
    blue stop. Used to paint the wordmark and any accent progress fills.
    """
    t = max(0.0, min(1.0, t))
    if t <= 0.5:
        start, end, local = GRAD_START, GRAD_MID, t / 0.5
    else:
        start, end, local = GRAD_MID, GRAD_END, (t - 0.5) / 0.5
    r1, g1, b1 = _hex_to_rgb(start)
    r2, g2, b2 = _hex_to_rgb(end)
    return _rgb_to_hex((_lerp(r1, r2, local), _lerp(g1, g2, local), _lerp(b1, b2, local)))


def color_enabled() -> bool:
    """Return True when ANSI color should be emitted.

    Honors the ``NO_COLOR`` convention (https://no-color.org) and suppresses
    color for non-TTY / dumb terminals. Evaluated lazily so tests and piped
    output stay deterministic.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def themed_style(style_dict: dict[str, str]) -> dict[str, str]:
    """Return *style_dict* as-is, or with every color token stripped.

    The fullscreen REPL's ``prompt_toolkit`` theme (and the palette/status-bar/
    model-switcher style dicts merged into it) are all built from this
    module's hex constants — every value is a space-separated token string
    like ``"bg:#0a0a0a #e8e8e6 bold"``. Route the assembled dict through this
    function immediately before ``Style.from_dict()`` so :func:`color_enabled`
    is the single, real gate on whether color reaches the terminal, instead
    of each call site hardcoding hex values that ``NO_COLOR`` never touches.

    Structural attributes (``bold``, ``italic``, ``underline``, ``reverse``,
    ``noinherit``, ...) are left in place — only ``#rrggbb`` and ``bg:#rrggbb``
    tokens are dropped — so emphasis and layout still read correctly in a
    monochrome terminal.
    """
    if color_enabled():
        return style_dict
    cleaned: dict[str, str] = {}
    for key, value in style_dict.items():
        tokens = [
            tok for tok in value.split() if not tok.startswith("#") and not tok.startswith("bg:#")
        ]
        cleaned[key] = " ".join(tokens)
    return cleaned
