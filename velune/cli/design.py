"""Central design tokens for the Velune CLI.

Organic/Natural earth-tone palette emphasizing orchestration, privacy, speed, and
intelligence. The design language celebrates multi-agent coordination with soft
tones and natural gradients — inspired by symphony orchestration and local-first
data privacy.

Palette:
- Deep teals (orchestration, control)
- Forest greens (local-first, privacy)
- Warm oranges (speed, energy)
- Muted earth tones (balance, stability)

Nothing here probes the terminal at import time; :func:`color_enabled` is
evaluated lazily so the palette degrades gracefully under ``NO_COLOR`` and on
dumb/unsupported terminals without affecting deterministic rendering.
"""

from __future__ import annotations

import os
import sys

# --- Brand palette: Organic/Natural Earth Tones ----------------------------
# Primary: Teal — represents orchestration, coordination, calm intelligence
ACCENT = "#2db8a4"  # deep teal (logo, primary brand, prompt prefix)
ACCENT_SOFT = "#4dd4c8"  # soft teal (secondary elements, arrows)

# Secondary: Forest Green — represents local-first, privacy, grounded security
PRIMARY_GREEN = "#1f8659"  # deep forest green (emphasis, highlights)
GREEN = "#2fb86a"  # vibrant forest green (accents, active states)

# Tertiary: Warm Orange — represents speed, energy, forward motion
HIGHLIGHT = "#d97e35"  # warm orange-brown (modes, speed indicators)
ENERGY = "#f5a94b"  # golden orange (active processes, speed)

# Info & feedback
INFO = "#5eb8d4"  # cooler teal for informational text
SUBTLE = "#3a9b7f"  # muted teal for subtle elements

# Semantic state colors (shared by status bar, badges, diffs).
OK = "#2fb86a"  # forest green — success
WARN = "#f5a94b"  # warm orange — warning
DANGER = "#d94d4d"  # muted red — danger

# Neutrals.
MUTED = "#7a9999"  # secondary text (warm gray-teal)
FAINT = "#5a7979"  # frame glyphs, separators (cooler)
SURFACE = "#0f1817"  # dark forest background
LIGHT_BG = "#1a2422"  # slightly lighter panels

# --- Semantic role aliases -------------------------------------------------
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
