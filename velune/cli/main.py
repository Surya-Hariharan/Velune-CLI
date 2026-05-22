"""CLI entry point."""

import typer
from rich.console import Console

app = typer.Typer(
    name="velune",
    help="Cognitive AI CLI for autonomous software engineering",
    no_args_is_help=True,
)

console = Console()


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", "-v", help="Show version"),
) -> None:
    """Velune CLI - Cognitive AI for software engineering."""
    if version:
        console.print("Velune v0.1.0")


# Import commands
from velune.cli.commands.run import run_cmd
from velune.cli.commands.models import models_cmd
from velune.cli.commands.workspace import workspace_cmd
from velune.cli.commands.memory import memory_cmd
from velune.cli.commands.config import config_cmd

# Register commands
app.command()(run_cmd)
app.add_typer(models_cmd, name="models")
app.add_typer(workspace_cmd, name="workspace")
app.add_typer(memory_cmd, name="memory")
app.add_typer(config_cmd, name="config")


if __name__ == "__main__":
    app()
