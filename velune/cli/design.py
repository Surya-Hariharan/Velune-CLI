"""Central design tokens for the Velune CLI.

Pink/White brand palette — a soft, modern, high-contrast look built around hot
pink accents on clean white text. The design language pairs a vivid magenta
brand hue with rose and blush tints for secondary elements, keeping warnings and
errors functionally distinct for readability.

Palette:
- Hot pink (primary brand, logo, prompt prefix)
- Blush / rose (secondary elements, active states)
- Deep magenta (emphasis, highlights)
- White & soft mauve neutrals (body text, separators)

Nothing here probes the terminal at import time; :func:`color_enabled` is
evaluated lazily so the palette degrades gracefully under ``NO_COLOR`` and on
dumb/unsupported terminals without affecting deterministic rendering.
"""

from __future__ import annotations

import os
import sys

# --- Brand palette: Pink & White -------------------------------------------
# Primary: Hot Pink — the signature brand hue (logo, prompt prefix, headings)
ACCENT = "#ff5fa2"  # hot pink (logo, primary brand, prompt prefix)
ACCENT_SOFT = "#ffa6cf"  # soft blush pink (secondary elements, arrows)

# Secondary: Deep Magenta — emphasis and strong highlights
PRIMARY_GREEN = "#e91e8c"  # deep magenta-pink (emphasis, highlights)
GREEN = "#ff7fb6"  # rose pink (accents, active states, success)

# Tertiary: Vivid Pink — modes, energy, forward motion
HIGHLIGHT = "#ff2d95"  # vivid magenta-pink (modes, indicators)
ENERGY = "#ffb3d9"  # light pink (active processes)

# Info & feedback
INFO = "#ff9ec7"  # soft pink for informational text
SUBTLE = "#c97a9c"  # muted mauve for subtle elements

# Semantic state colors (shared by status bar, badges, diffs). Warnings/errors
# stay functionally legible while sitting comfortably inside the pink theme.
OK = "#ff7fb6"  # rose pink — success
WARN = "#ffb86b"  # warm peach — warning (kept distinct for legibility)
DANGER = "#ff4d6d"  # hot red-pink — danger

# Neutrals.
WHITE = "#ffffff"  # primary body text
MUTED = "#d9a8c0"  # secondary text (soft mauve-pink)
FAINT = "#9a6f82"  # frame glyphs, separators (dim mauve)
SURFACE = "#1a0d14"  # very dark plum background
LIGHT_BG = "#2a1520"  # slightly lighter plum panels

# --- Semantic role aliases -------------------------------------------------
PINK = ACCENT
SUCCESS = OK
ERROR = DANGER
ACCENT_TEXT = ACCENT
CONTROL = ACCENT  # orchestration/control
PRIVACY = PRIMARY_GREEN  # local-first, secure
SPEED = HIGHLIGHT  # performance, energy

# Context-pressure thresholds (percent of context window consumed). Shared by
# the prompt badge, the bottom status bar, and the /context command so all
# three agree on what "getting full" means.
CTX_WARN_PCT = 70.0
CTX_DANGER_PCT = 90.0


def context_state(pct: float) -> str:
    """Map a context-usage percentage to a semantic state name (ok/warn/danger)."""
    if pct < CTX_WARN_PCT:
        return "ok"
    if pct < CTX_DANGER_PCT:
        return "warn"
    return "danger"


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
