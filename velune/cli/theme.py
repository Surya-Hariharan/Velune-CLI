"""Compatibility shim — the canonical palette now lives in ``velune.cli.design``.

All color constants re-exported here so existing importers get the correct hex
values from design.py without requiring a mass rename.
"""

from velune.cli.design import (  # noqa: F401
    ACCENT,
    ACCENT_SOFT,
    FAINT,
    HIGHLIGHT,
    INFO,
    SURFACE,
)

CODE_BG = "#2e2e2e"
