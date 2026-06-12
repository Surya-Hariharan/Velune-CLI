"""Rich terminal themes."""

from rich.theme import Theme


class VeluneTheme:
    """Velune CLI theme — modern, restrained color palette."""

    @staticmethod
    def get_theme() -> Theme:
        """Get the Velune theme with sophisticated color choices."""
        return Theme({
            "info": "blue",              # Primary blue for info
            "warning": "yellow",         # Warm yellow for warnings
            "error": "red",              # Clear red for errors
            "success": "green",          # Green for success
            "title": "bold blue",        # Primary blue titles
            "subtitle": "dim blue",      # Subtle secondary blue
            "key": "dim white",          # Subtle key labels
            "value": "white",            # Clear values
            "muted": "dim",              # Background/muted text
            "accent": "#d4af37",         # Warm gold accent
        })
