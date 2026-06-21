"""Rich-based terminal panels."""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


class DisplayPanels:
    """Utility class for creating rich display panels."""

    def __init__(self, console: Console):
        self.console = console

    def info_panel(self, title: str, content: str) -> None:
        """Display an info panel."""
        self.console.print(Panel(content, title=title, border_style="blue"))

    def success_panel(self, title: str, content: str) -> None:
        """Display a success panel."""
        self.console.print(Panel(content, title=title, border_style="green"))

    def warning_panel(self, title: str, content: str) -> None:
        """Display a warning panel."""
        self.console.print(Panel(content, title=title, border_style="yellow"))

    def error_panel(self, title: str, content: str) -> None:
        """Display an error panel."""
        self.console.print(Panel(content, title=title, border_style="red"))

    def create_table(self, title: str, columns: list[str]) -> Table:
        """Create a rich table."""
        table = Table(title=title)
        for column in columns:
            table.add_column(column)
        return table
