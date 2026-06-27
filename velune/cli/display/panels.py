"""Rich-based terminal panels — thin wrappers around velune.cli.ui.

All concrete rendering logic lives in ``velune.cli.ui``.  This module exists
for backwards-compatibility with code that imports ``DisplayPanels``.
"""

from rich.console import Console
from rich.table import Table

from velune.cli import ui


class DisplayPanels:
    """Utility class for creating rich display panels.

    Delegates to ``velune.cli.ui`` for all rendering so every panel in the
    application uses consistent borders, colours, and spacing.
    """

    def __init__(self, console: Console) -> None:
        self.console = console

    def info_panel(self, title: str, content: str) -> None:
        self.console.print(ui.panel(content, title=title, kind="info"))

    def success_panel(self, title: str, content: str) -> None:
        self.console.print(ui.success_panel(title, content))

    def warning_panel(self, title: str, content: str) -> None:
        self.console.print(ui.warning_panel(title, content))

    def error_panel(self, title: str, content: str) -> None:
        from rich.text import Text
        self.console.print(ui.error_panel(title, cause=content))

    def create_table(self, title: str, columns: list[str]) -> Table:
        tv = ui.TableView(columns, title=title)
        return tv.render()
