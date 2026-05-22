"""Rich-based progress display."""

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn


class ProgressDisplay:
    """Utility class for displaying progress."""

    def __init__(self, console: Console):
        self.console = console

    def create_progress(self) -> Progress:
        """Create a rich progress bar."""
        return Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=self.console,
        )

    def spinner(self, description: str):
        """Create a spinner context manager."""
        return self.console.status(description)
