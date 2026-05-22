"""Models command - velune models scan/list/assign."""

import typer
from rich.console import Console
from rich.table import Table

console = Console()

models_cmd = typer.Typer(help="Model management commands")


@models_cmd.command("scan")
def models_scan(
    provider: str = typer.Option(None, "--provider", "-p", help="Specific provider to scan"),
) -> None:
    """Scan for available models."""
    console.print("[yellow]Model scanning not yet implemented.[/yellow]")
    console.print("This will auto-discover models from:")
    console.print("  • Ollama (localhost:11434)")
    console.print("  • LM Studio (localhost:1234)")
    console.print("  • OpenAI (via API)")
    console.print("  • Anthropic (via API)")
    console.print("  • Hugging Face (via API)")
    console.print("  • Local GGUF files")


@models_cmd.command("list")
def models_list() -> None:
    """List registered models."""
    console.print("[yellow]Model listing not yet implemented.[/yellow]")
    
    table = Table(title="Registered Models")
    table.add_column("ID", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Provider", style="magenta")
    table.add_column("Capabilities", style="blue")
    
    console.print(table)


@models_cmd.command("assign")
def models_assign(
    role: str = typer.Argument(..., help="Agent role (planner, coder, reasoner, etc.)"),
    model_id: str = typer.Argument(..., help="Model ID to assign"),
) -> None:
    """Assign a model to an agent role."""
    console.print(f"[yellow]Assigning model {model_id} to role {role}[/yellow]")
    console.print("[yellow]Model assignment not yet implemented.[/yellow]")
