"""Central design tokens for the Velune CLI.

This module is the single source of truth for Velune's terminal palette and
spacing. Historically colors were hardcoded as raw hex (``#c084fc``,
``#a78bfa``, ``#d4af37``) scattered across ``banner.py``, ``repl.py`` and
``statusbar.py``, which drifted out of sync (e.g. the help table rendered in
``blue`` while everything else used ``cyan``/``dim``). Routing every surface
through these semantic tokens keeps the product visually coherent and makes a
future theme switch a one-file change.

Nothing here probes the terminal at import time; :func:`color_enabled` is
evaluated lazily so the palette degrades gracefully under ``NO_COLOR`` and on
dumb/unsupported terminals without affecting deterministic rendering.
"""

from __future__ import annotations

import os
import sys

# --- Brand palette ---------------------------------------------------------
# A calm purple-lavender accent with a single gold highlight. Kept intentionally
# small: one accent, one highlight, three semantic states, two neutrals.
ACCENT = "#c084fc"  # primary brand purple (logo, prompt prefix)
ACCENT_SOFT = "#a78bfa"  # secondary lavender (arrows, rules, active glyphs)
HIGHLIGHT = "#d4af37"  # gold — modes, emphasis
INFO = "#5fd7ff"  # cyan — workspace / informational

# Semantic state colors (shared by status bar, badges, diffs).
OK = "#5fd787"
WARN = "#ffaf00"
DANGER = "#ff5f5f"

# Neutrals.
MUTED = "#8a8a9a"  # secondary text on a panel
FAINT = "#6a6a7a"  # frame glyphs, separators
SURFACE = "#1c1c28"  # status-bar / panel background

# --- Semantic role aliases -------------------------------------------------
# Prefer these names in call sites so intent survives a palette change.
SUCCESS = OK
ERROR = DANGER
ACCENT_TEXT = ACCENT

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
