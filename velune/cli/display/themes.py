"""Rich terminal theme — driven by ``velune.cli.design`` tokens.

Maps every semantic name used in Rich markup throughout the codebase to the
canonical hex value from ``design.py``.  Import VeluneTheme and call
``get_theme()`` once when constructing a Console; never hard-code colour
strings in rendering code.
"""

from rich.theme import Theme

from velune.cli import design


class VeluneTheme:
    """Velune CLI theme.  All colours derive from ``velune.cli.design``."""

    @staticmethod
    def get_theme() -> Theme:
        """Return a Rich Theme mapping semantic names to design-token hex values."""
        d = design
        return Theme(
            {
                # ── Semantic state ─────────────────────────────────────────
                "info": d.INFO,
                "warning": d.WARN,
                "error": d.DANGER,
                "success": d.OK,
                # ── Typography ────────────────────────────────────────────
                "title": f"bold {d.WHITE}",
                "subtitle": f"dim {d.MUTED}",
                "label": f"bold {d.MUTED}",
                "body": d.WHITE,
                "muted": d.MUTED,
                "faint": d.FAINT,
                "dim": f"dim {d.MUTED}",
                "key": f"dim {d.FAINT}",
                "value": d.WHITE,
                # ── Brand / accent ────────────────────────────────────────
                "accent": d.ACCENT,
                "accent.soft": d.ACCENT_SOFT,
                "velune.accent": d.ACCENT,
                "velune.soft": d.ACCENT_SOFT,
                "velune.pink": d.ACCENT,
                # ── Structural roles ──────────────────────────────────────
                "border": d.FAINT,
                "separator": d.FAINT,
                "highlight": d.HIGHLIGHT,
                "selected": f"bold {d.WHITE}",
                "selected.bg": d.ACCENT,
                # ── Badge / tag colours ───────────────────────────────────
                "badge.ok": d.OK,
                "badge.warn": d.WARN,
                "badge.error": d.DANGER,
                "badge.info": d.INFO,
                # ── Status bar semantic names (used by statusbar.py) ──────
                "status.model": f"bold {d.ACCENT}",
                "status.mode": d.HIGHLIGHT,
                "status.ok": d.OK,
                "status.warn": d.WARN,
                "status.danger": d.DANGER,
                "status.speed": d.ENERGY,
                "status.privacy": d.PRIMARY_GREEN,
                # ── Provider / job state ──────────────────────────────────
                "state.running": d.WARN,
                "state.completed": d.OK,
                "state.failed": d.DANGER,
                "state.cancelled": d.FAINT,
                "state.pending": d.INFO,
                # ── Diff / patch ──────────────────────────────────────────
                "diff.add": d.OK,
                "diff.remove": d.DANGER,
                "diff.hunk": d.INFO,
            }
        )
