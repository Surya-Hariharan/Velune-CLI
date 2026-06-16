"""Rich terminal themes."""

from rich.theme import Theme

from velune.cli import design


class VeluneTheme:
    """Velune CLI theme — driven by the canonical palette in ``velune.cli.design``."""

    @staticmethod
    def get_theme() -> Theme:
        """Return a Rich Theme whose semantic names map to design.py hex values."""
        return Theme(
            {
                "info": design.INFO,
                "warning": design.WARN,
                "error": design.DANGER,
                "success": design.OK,
                "title": f"bold {design.INFO}",
                "subtitle": f"dim {design.INFO}",
                "key": "dim white",
                "value": "white",
                "muted": design.MUTED,
                "accent": design.HIGHLIGHT,
                "velune.accent": design.ACCENT,
                "velune.soft": design.ACCENT_SOFT,
            }
        )
