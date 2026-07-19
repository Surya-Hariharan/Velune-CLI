"""Interactive ask command boundary — routes natural language questions to the Council."""

from __future__ import annotations

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from velune.cognition.firewall import CognitiveFirewall
from rich.console import Console

from velune.cli.context import CLIContext

if TYPE_CHECKING:
    from velune.repository.schemas import RepositorySnapshot

console = Console()
ask_cmd = typer.Typer(help="Interactive prompt entry point")


def ask_command(
    ctx: typer.Context,
    prompt: str | None = typer.Argument(None, help="Question or task to route through Velune"),
    council_tier: str | None = typer.Option(
        None, "--council-tier", help="Override council execution tier (instant, standard, full)"
    ),
) -> None:
    """Ask a question or request a code review (no code execution)."""

    if ctx.invoked_subcommand is not None:
        return

    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    if not prompt:
        if cli_context.json_mode:
            import json

            print(json.dumps({"error": "Prompt argument is required in JSON mode"}))
            raise typer.Exit(code=1)
        # Prompt user interactively if no prompt argument is given
        prompt = typer.prompt("What would you like to ask Velune?")
        if not prompt:
            console.print("[yellow]Empty query. Exiting.[/yellow]")
            return

    from velune.core.event_loop import submit

    submit(_ask_command_async(cli_context, prompt, council_tier))


async def _ask_command_async(
    cli_context: CLIContext, prompt: str, council_tier: str | None = None
) -> None:
    lifecycle = cli_context.container.get("runtime.lifecycle")

    # 2. Boot subsystems
    if not cli_context.json_mode:
        console.print("[dim]Thinking…[/dim]")
    await lifecycle.startup()
    try:
        await _ask_with_runtime(cli_context, prompt, council_tier)
    finally:
        # Shutdown must run on every exit path — including typer.Exit and
        # council errors — or background tasks outlive the command.
        await lifecycle.shutdown()


async def _ask_with_runtime(
    cli_context: CLIContext, prompt: str, council_tier: str | None = None
) -> None:
    container = cli_context.container
    model_registry = container.get("runtime.model_registry")
    model_specialization = container.get("runtime.council_orchestrator").mapper
    repo_cognition = container.get("runtime.repository_cognition")
    orchestrator = container.get("runtime.council_orchestrator")

    await model_registry.refresh()

    # Onboarding preflight gate. `ask` is a one-off question, so it works in any
    # directory — no git repo or index required. Only a reachable model is
    # enforced (require_workspace=False).
    from velune.cli.commands.preflight import run_preflight_check

    if not await run_preflight_check(
        container,
        console if not cli_context.json_mode else None,
        require_workspace=False,
    ):
        if cli_context.json_mode:
            import json

            print(
                json.dumps(
                    {"error": "No model available. Run `velune setup` to configure a provider."}
                )
            )
        raise typer.Exit(code=1)

    if not cli_context.json_mode:
        from velune.cli.display.council_view import CouncilDisplayView

        display = CouncilDisplayView(console)
        display.render_header(prompt)

    # 3. Map role specializations
    roles = model_specialization.map_roles()
    if not cli_context.json_mode:
        display.render_role_assignments(roles)

    from velune.cognition.firewall import CognitiveFirewall

    firewall = CognitiveFirewall()

    # 4. Ingest and Scan AST Snapshot — only when the current directory is
    # actually a project. A one-off question asked from an empty directory
    # should not index arbitrary files; it answers as a general question.
    snapshot: RepositorySnapshot | None = None
    if _looks_like_project(cli_context.workspace):
        if not cli_context.json_mode:
            with console.status("[bold magenta]Scanning codebase AST structure...[/bold magenta]"):
                snapshot = repo_cognition.index()
        else:
            snapshot = repo_cognition.index()

    if snapshot is not None:
        formatted_snap = _format_snapshot_context_safe(snapshot, firewall)
    else:
        formatted_snap = "No repository context — answering as a general question."

    # 5. deliberating debate loop
    council_res = None
    if not cli_context.json_mode:
        with console.status(
            "[bold magenta]Deliberating Reasoning Council debate...[/bold magenta]"
        ):
            council_res = await orchestrator.execute_task(
                prompt, formatted_snap, council_tier=council_tier
            )
    else:
        council_res = await orchestrator.execute_task(
            prompt, formatted_snap, council_tier=council_tier
        )

    arbitration = council_res["arbitration"]
    final_summary = council_res["final_summary"]

    # Total failure (wall-time exhausted or every agent call failed) is an
    # error, not an answer: one actionable message and a non-zero exit code
    # instead of rendering a degraded report and returning success.
    if council_res.get("is_timeout"):
        _render_council_failure(cli_context, final_summary, arbitration.get("flags", []))
        raise typer.Exit(code=1)

    if cli_context.json_mode:
        import json

        def _dump(report: object) -> object:
            dump = getattr(report, "model_dump", None)
            return dump() if callable(dump) else report

        roles_dict = {role.value: model_id for role, model_id in roles.items()}
        print(
            json.dumps(
                {
                    "prompt": prompt,
                    "roles": roles_dict,
                    "reviewer_report": _dump(council_res["reviewer_report"]),
                    "challenger_report": _dump(council_res["challenger_report"]),
                    "arbitration": arbitration,
                    "final_summary": final_summary,
                },
                default=str,
            )
        )
    else:
        # 6. Render reports. Reviewer/Challenger are skipped entirely (not
        # just their content) below tier 3 — CouncilOrchestrator's early
        # return for those tiers leaves both as None by design, and showing
        # a "deliberating..." header for a step that never ran is misleading.
        if council_res["reviewer_report"] is not None:
            display.render_step_header("Council Reviewer")
            display.render_reviewer_report(council_res["reviewer_report"])

        if council_res["challenger_report"] is not None:
            display.render_step_header("Council Challenger")
            display.render_challenger_report(council_res["challenger_report"])

        display.render_step_header("Arbitration Engine")
        display.render_arbitration_result(arbitration)

        display.render_step_header("Council Synthesizer")
        display.render_synthesized_response(final_summary)

        from velune.cli import guidance, ui

        short_task = prompt if len(prompt) <= 40 else prompt[:40].rstrip() + "…"
        steps = guidance.steps_for("ask_completed", task=f'"{short_task}"')
        if steps:
            console.print(
                ui.next_steps(
                    "Council answered",
                    "Turn this deliberation into action.",
                    steps,
                )
            )


def _render_council_failure(cli_context: CLIContext, summary: str, flags: list[str]) -> None:
    """Render a single actionable error for a run that produced no answer."""
    message = summary or "The council could not produce an answer."

    invalid: list[str] = []
    try:
        from velune.providers.keystore import list_invalid_providers

        invalid = list_invalid_providers()
    except Exception:
        pass

    if cli_context.json_mode:
        import json

        print(json.dumps({"error": message, "flags": flags, "invalid_providers": invalid}))
        return

    from rich.box import ROUNDED
    from rich.panel import Panel
    from rich.text import Text

    lines = [f"[bold red]{message}[/bold red]\n"]
    if invalid:
        for pid in invalid:
            lines.append(
                f"\nYour [bold]{pid}[/bold] API key was rejected by the provider.\n"
                f"  [bold white]Fix:[/bold white] [bold green]velune provider add {pid}[/bold green]"
            )
    else:
        lines.append(
            "\nCommon causes: an invalid or expired API key, or an unreachable provider.\n"
            "  [bold white]Check:[/bold white] [bold green]velune doctor check[/bold green]"
            "  [dim]or[/dim]  [bold green]velune providers[/bold green]"
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


# Markers that indicate the workspace is (probably) a real project root, in
# which case repository context is worth indexing for the answer.
_PROJECT_MARKERS = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod")


def _looks_like_project(workspace: object) -> bool:
    """Return True if *workspace* contains a recognizable project marker."""
    from pathlib import Path

    try:
        root = Path(str(workspace))
        return any((root / marker).exists() for marker in _PROJECT_MARKERS)
    except Exception:
        return False


def _format_snapshot_context_safe(snapshot: RepositorySnapshot, firewall: CognitiveFirewall) -> str:
    """Format snapshot metadata context for query prompt securely."""
    lines = [f"Repository Root: {snapshot.root_path}"]
    lines.append("Codebase Files:")
    for f in snapshot.files[:25]:
        # Only expose path and language — no raw symbol names or content
        risk_marker = " [injection-risk]" if f.metadata.get("injection_risk") else ""
        lines.append(f"  - {f.path} ({f.language.value}){risk_marker}")
        # Symbol names are safe to expose (identifiers, not content)
        if f.symbols:
            safe_syms = [s.name for s in f.symbols[:3] if s.name.isidentifier()]
            if safe_syms:
                lines.append(f"    Symbols: {', '.join(safe_syms)}")
    return "\n".join(lines)
