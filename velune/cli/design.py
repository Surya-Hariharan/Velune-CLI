"""Central design tokens for the Velune CLI.

Monochrome palette — grayscale text and structure on a near-black background,
with a single restrained accent (soft steel-blue) reserved for the logo,
prompt, and active/selected state. Semantic colors (ok/warn/danger) stay
desaturated so they read as "muted amber" or "muted rust" rather than neon,
while remaining functionally distinct for legibility.

Palette:
- Grayscale neutrals (body text, separators, panels)
- One accent hue, used sparingly (logo, prompt prefix, active states)
- Desaturated semantic colors (success / warning / danger)

Nothing here probes the terminal at import time; :func:`color_enabled` is
evaluated lazily so the palette degrades gracefully under ``NO_COLOR`` and on
dumb/unsupported terminals without affecting deterministic rendering.
"""

from __future__ import annotations

import os
import sys

# --- Brand palette: vibrant indigo accent + cool gradient --------------------
# One vivid hue drives the whole theme — the logo wordmark, prompt glyph,
# headings, and active/selected state — set against grayscale neutrals so the
# accent reads as energetic without turning the UI into noise.
ACCENT = "#818cf8"  # electric indigo (wordmark, primary brand, prompt prefix)
ACCENT_SOFT = "#5b63d6"  # dimmer indigo (secondary elements, arrows)

# The brand wordmark is painted as a horizontal gradient across these three
# stops (violet → blue → teal). `gradient_hex(t)` interpolates between them for
# any t in [0, 1]; other surfaces can reuse it for progress fills, meters, etc.
GRAD_START = "#a78bfa"  # violet
GRAD_MID = "#60a5fa"  # blue
GRAD_END = "#2dd4bf"  # teal

# Reuses of the single accent — kept as separate names because other modules
# reference them by role, not because they carry a distinct hue. They point at
# the accent tokens (not hardcoded copies) so a recolor here propagates.
PRIMARY_GREEN = ACCENT_SOFT  # (emphasis, highlights)
GREEN = "#7a9b82"  # = OK (accents, active states, success)

HIGHLIGHT = ACCENT  # (modes, indicators)
ENERGY = ACCENT_SOFT  # (active processes)

# Info & feedback — desaturated, accent-tinted gray rather than a new hue.
INFO = "#96a8ae"  # muted steel-gray for informational text
SUBTLE = "#7a7a78"  # muted gray for subtle elements

# Semantic state colors (shared by status bar, badges, diffs). Desaturated so
# they sit quietly in the monochrome theme while staying legible.
OK = "#7a9b82"  # muted sage — success
WARN = "#b3966e"  # muted amber — warning
DANGER = "#b3706e"  # muted brick red — danger

# Neutrals.
BACKGROUND = "#0a0a0a"  # fullscreen REPL background
WHITE = "#e8e8e6"  # primary body text (soft off-white, not pure #fff)
SECONDARY = "#a3a3a1"  # neutral secondary text
MUTED = "#7a7a78"  # secondary/dim text
FAINT = "#4a4a48"  # frame glyphs, separators
SURFACE = "#131311"  # panel background
LIGHT_BG = "#1e1e1c"  # slightly lighter panels

# --- Semantic role aliases -------------------------------------------------
# NOTE: "PINK" is a legacy name from the previous brand palette — it now
# points at the single monochrome accent, not an actual pink hue. Left
# unrenamed to avoid a mass rename across every importer for a recolor-only
# pass; rename if this theme becomes permanent.
PINK = ACCENT
SUCCESS = OK
ERROR = DANGER
ACCENT_TEXT = ACCENT
CONTROL = ACCENT  # orchestration/control
PRIVACY = PRIMARY_GREEN  # local-first, secure
SPEED = HIGHLIGHT  # performance, energy

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
