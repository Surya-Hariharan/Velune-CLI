"""``velune ask`` — answer a question or review code with the agent swarm (nothing is changed)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, NoReturn

import typer
from rich.console import Console

from velune.cli.context import CLIContext

if TYPE_CHECKING:
    from velune.swarm.run import SwarmOutcome

console = Console()
ask_cmd = typer.Typer(help="Interactive prompt entry point")


def ask_command(
    ctx: typer.Context,
    prompt: str | None = typer.Argument(None, help="Question or task to route through Velune"),
    skill: str | None = typer.Option(None, "--skill", help="Skill to use (see /skills)"),
    permission: str | None = typer.Option(
        None, "--permission", help="Permission profile to use (see /skills)"
    ),
    council_tier: str | None = typer.Option(None, "--council-tier", hidden=True),
    explain_trace: bool = typer.Option(
        False,
        "--explain-trace",
        help="Print the team that ran, and how each agent finished.",
    ),
) -> None:
    """Ask a question or request a code review (nothing is written or run)."""

    if ctx.invoked_subcommand is not None:
        return

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    # Whitespace-only input is empty: it would otherwise go through the whole
    # provider setup before failing.
    prompt = prompt.strip() if prompt else prompt
    if not prompt:
        if cli_context.json_mode:
            import json

            print(json.dumps({"error": "Prompt argument is required in JSON mode"}))
            raise typer.Exit(code=1)
        prompt = typer.prompt("What would you like to ask Velune?").strip()
        if not prompt:
            console.print("[yellow]Empty query. Exiting.[/yellow]")
            return
    if council_tier and not cli_context.json_mode:
        console.print("[dim]--council-tier no longer has an effect; use --skill instead.[/dim]")

    from velune.core.event_loop import submit

    submit(_ask_command_async(cli_context, prompt, skill, permission, explain_trace))


async def _ask_command_async(
    cli_context: CLIContext,
    prompt: str,
    skill: str | None = None,
    permission: str | None = None,
    explain_trace: bool = False,
) -> None:
    lifecycle = cli_context.container.get("runtime.lifecycle")
    if not cli_context.json_mode:
        console.print("[dim]Thinking…[/dim]")
    await lifecycle.startup()
    try:
        await _ask_with_runtime(cli_context, prompt, skill, permission, explain_trace)
    finally:
        # Shutdown runs on every exit path, typer.Exit included, or background
        # tasks outlive the command.
        await lifecycle.shutdown()


async def _ask_with_runtime(
    cli_context: CLIContext,
    prompt: str,
    skill: str | None = None,
    permission: str | None = None,
    explain_trace: bool = False,
) -> None:
    from velune.cli.commands.preflight import run_preflight_check
    from velune.cli.model_prefs import load_active_model
    from velune.swarm.router import RouteError

    container = cli_context.container
    await container.get("runtime.model_registry").refresh()

    # `ask` is a one-off question, so it works in any directory: only a
    # reachable model is required (require_workspace=False).
    if not await run_preflight_check(
        container,
        console if not cli_context.json_mode else None,
        require_workspace=False,
    ):
        _fail(cli_context, "No model available. Run `velune setup` to configure a provider.")

    context = await _context(cli_context, prompt)
    try:
        if cli_context.json_mode:
            outcome = await _run(container, prompt, skill, permission, context, load_active_model())
        else:
            with console.status("[bold magenta]The council is working…[/bold magenta]"):
                outcome = await _run(
                    container, prompt, skill, permission, context, load_active_model()
                )
    except (RouteError, RuntimeError) as exc:
        _fail(cli_context, str(exc))

    if explain_trace and not cli_context.json_mode:
        from velune.cli.display.swarm_view import plan_table

        console.print(plan_table(outcome.plan))
        for note in outcome.plan.notes:
            console.print(f"[dim]· {note}[/dim]")
        for result in outcome.results:
            detail = f": {result.error}" if result.error else ""
            console.print(f"[dim]{result.agent_id} · {result.status}{detail}[/dim]")

    if cli_context.json_mode:
        import json

        print(json.dumps(outcome_json(prompt, outcome)))
        if not outcome.ok:
            raise typer.Exit(code=1)
        return
    if not outcome.ok:
        _render_council_failure(cli_context, outcome.error)
        raise typer.Exit(code=1)

    from velune.cli import guidance, ui
    from velune.cli.display.swarm_view import outcome_lines
    from velune.cli.rendering import CustomMarkdown

    console.print()
    console.print(CustomMarkdown(outcome.content))
    for line in outcome_lines(outcome):
        console.print(line, markup=False)
    short_task = prompt if len(prompt) <= 40 else prompt[:40].rstrip() + "…"
    steps = guidance.steps_for("ask_completed", task=f'"{short_task}"')
    if steps:
        console.print(ui.next_steps("Council answered", "Turn this answer into action.", steps))


async def _run(container, prompt, skill, permission, context, preferred) -> SwarmOutcome:
    from velune.permissions import ExecutionMode
    from velune.swarm.setup import run_headless

    return await run_headless(
        container,
        prompt,
        mode=ExecutionMode.PLAN,
        skill=skill,
        permission=permission,
        context=context,
        preferred=preferred,
    )


def outcome_json(prompt: str, outcome: SwarmOutcome) -> dict[str, Any]:
    return {
        "prompt": prompt,
        "status": outcome.status,
        "response": outcome.content,
        "error": outcome.error,
        "skill": outcome.plan.skill,
        "permission": outcome.plan.permission,
        "output": outcome.plan.output,
        "notes": outcome.plan.notes,
        "run_id": outcome.run_id,
        "files_changed": outcome.files_changed,
        "diagram": str(outcome.diagram_path) if outcome.diagram_path else None,
        "warnings": outcome.warnings,
        "agents": [
            {"id": r.agent_id, "status": r.status, "error": r.error} for r in outcome.results
        ],
    }


async def _context(cli_context: CLIContext, prompt: str) -> str:
    """Repository summary (only inside a project) plus relevant memory, both untrusted."""
    from velune.cli.handlers.prompt_context import retrieve_memory_context
    from velune.swarm.setup import repository_summary, wrap_untrusted

    container = cli_context.container
    parts = []
    if _looks_like_project(cli_context.workspace):
        parts.append(await repository_summary(container))
    else:
        parts.append("No repository context: answer as a general question.")
    # The same read-only memory retrieval the REPL uses, so a one-off `ask`
    # also benefits from what Velune remembers.
    chunks = await retrieve_memory_context(container, prompt, cli_context.workspace)
    if chunks:
        memory = "\n\n".join(f"[{c.source}] {c.content}" for c in chunks)
        parts.append("Relevant memory:\n" + wrap_untrusted(container, "retrieved_memory", memory))
    return "\n\n".join(p for p in parts if p)


def _fail(cli_context: CLIContext, message: str) -> NoReturn:
    _render_council_failure(cli_context, message)
    raise typer.Exit(code=1)


def _invalid_providers() -> list[str]:
    try:
        from velune.providers.keystore import list_invalid_providers

        return list(list_invalid_providers())
    except Exception:
        return []


def _render_council_failure(cli_context: CLIContext, message: str) -> None:
    """One actionable error for a run that produced no answer."""
    message = message or "The council could not produce an answer."
    invalid = _invalid_providers()
    if cli_context.json_mode:
        import json

        print(json.dumps({"error": message, "invalid_providers": invalid}))
        return

    from rich.box import ROUNDED
    from rich.markup import escape
    from rich.panel import Panel
    from rich.text import Text

    lines = [f"[bold red]{escape(message)}[/bold red]\n"]
    if invalid:
        for pid in invalid:
            lines.append(
                f"\nYour [bold]{pid}[/bold] API key was rejected by the provider.\n"
                f"  [bold white]Fix:[/bold white] [bold green]velune provider add {pid}[/bold green]"
            )
    else:
        lines.append(
            "\nCommon causes: an invalid or expired API key, a model the provider no "
            "longer serves (HTTP 404), or an unreachable provider.\n"
            "  [bold white]Check:[/bold white] [bold green]velune doctor check[/bold green]"
            "  [dim]or[/dim]  [bold green]velune provider list[/bold green]\n"
            "  [bold white]Stale model?[/bold white] [bold green]velune models refresh[/bold green]"
        )
    console.print()
    console.print(
        Panel(
            Text.from_markup("".join(lines)),
            title="[bold red]Council Failed[/bold red]",
            border_style="red",
            box=ROUNDED,
            padding=(1, 2),
        )
    )
    console.print()


# Markers of a real project root, where repository context is worth reading.
_PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")


def _looks_like_project(workspace: object) -> bool:
    from pathlib import Path

    try:
        root = Path(str(workspace))
        return any((root / marker).exists() for marker in _PROJECT_MARKERS)
    except Exception:
        return False
