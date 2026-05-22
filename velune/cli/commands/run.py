"""Run command - velune run <task>."""

import typer
from pathlib import Path
from rich.console import Console
from rich.panel import Panel

console = Console()


def run_cmd(
    task: str = typer.Argument(..., help="Task description"),
    workspace: Path = typer.Option(
        Path.cwd(),
        "--workspace",
        "-w",
        help="Workspace directory",
    ),
    model: str = typer.Option(None, "--model", "-m", help="Model to use"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output"),
) -> None:
    """Run a task with Velune."""
    console.print(
        Panel.fit(
            f"[bold]Task:[/bold] {task}\n"
            f"[bold]Workspace:[/bold] {workspace}\n"
            f"[bold]Model:[/bold] {model or 'auto'}",
            title="Velune Run",
        )
    )
    
    console.print("\n[yellow]Task execution is not yet fully implemented.[/yellow]")
    console.print("The architecture has been set up. Key components:")
    console.print("  ✓ Provider abstraction layer")
    console.print("  ✓ Model registry and routing")
    console.print("  ✓ Memory architecture (working, episodic, semantic, procedural, graph)")
    console.print("  ✓ Context management and prioritization")
    console.print("  ✓ Hybrid retrieval (vector, lexical, graph)")
    console.print("  ✓ Repository cognition with AST parsing")
    console.print("  ✓ Tool system (filesystem, git, terminal, code, web)")
    console.print("\nRemaining components to implement:")
    console.print("  ○ Agent system with protocol-based communication")
    console.print("  ○ Orchestration engine with LangGraph")
    console.print("  ○ Execution pipeline with sandboxing and rollback")
    console.print("  ○ Workspace cognition and state machine")
    console.print("  ○ Event-driven cognition system")
