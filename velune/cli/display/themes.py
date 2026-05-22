"""Rich terminal themes."""

from rich.theme import Theme


class VeluneTheme:
    """Velune CLI theme."""

    @staticmethod
    def get_theme() -> Theme:
        """Get the Velune theme."""
        return Theme({
            "info": "cyan",
            "warning": "yellow",
            "error": "red",
            "success": "green",
            "title": "bold blue",
            "subtitle": "bold cyan",
            "key": "magenta",
            "value": "green",
        })
