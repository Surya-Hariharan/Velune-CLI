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
    # 1. Access services from DI container
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    model_registry = container.get("runtime.model_registry")
    model_specialization = container.get("runtime.council_orchestrator").mapper
    repo_cognition = container.get("runtime.repository_cognition")
    orchestrator = container.get("runtime.council_orchestrator")

    # 2. Boot subsystems
    if not cli_context.json_mode:
        console.print("[bold cyan]⠋[/bold cyan] Bootstrapping Cognitive Operating System kernel...")
    await lifecycle.startup()
    await model_registry.refresh()

    # Onboarding preflight check gate
    from velune.cli.commands.preflight import run_preflight_check

    if not await run_preflight_check(container, console if not cli_context.json_mode else None):
        if cli_context.json_mode:
            import json

            print(
                json.dumps(
                    {
                        "error": "Preflight check failed. Ensure workspace is initialized and models are scanned."
                    }
                )
            )
        await lifecycle.shutdown()
        return

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

    # 4. Ingest and Scan AST Snapshot
    snapshot: RepositorySnapshot | None = None
    if not cli_context.json_mode:
        with console.status("[bold magenta]⚡ Scanning codebase AST structure...[/bold magenta]"):
            snapshot = repo_cognition.index()
    else:
        snapshot = repo_cognition.index()

    formatted_snap = _format_snapshot_context_safe(snapshot, firewall)

    # 5. deliberating debate loop
    council_res = None
    if not cli_context.json_mode:
        with console.status(
            "[bold magenta]🧠 Deliberating Reasoning Council debate...[/bold magenta]"
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

    if cli_context.json_mode:
        import json

        roles_dict = {role.value: model_id for role, model_id in roles.items()}
        print(
            json.dumps(
                {
                    "prompt": prompt,
                    "roles": roles_dict,
                    "reviewer_report": council_res["reviewer_report"],
                    "challenger_report": council_res["challenger_report"],
                    "arbitration": arbitration,
                    "final_summary": final_summary,
                }
            )
        )
    else:
        # 6. Render reports
        display.render_step_header("Council Reviewer", "🔍")
        display.render_reviewer_report(council_res["reviewer_report"])

        display.render_step_header("Council Challenger", "⚡")
        display.render_challenger_report(council_res["challenger_report"])

        display.render_step_header("Arbitration Engine", "⚖️")
        display.render_arbitration_result(arbitration)

        display.render_step_header("Council Synthesizer", "🚀")
        display.render_synthesized_response(final_summary)

    # 7. Shutdown
    await lifecycle.shutdown()


def _format_snapshot_context_safe(snapshot: RepositorySnapshot, firewall: CognitiveFirewall) -> str:
    """Format snapshot metadata context for query prompt securely."""
    lines = [f"Repository Root: {snapshot.root_path}"]
    lines.append("Codebase Files:")
    for f in snapshot.files[:25]:
        # Only expose path and language — no raw symbol names or content
        risk_marker = " [⚠ injection-risk]" if f.metadata.get("injection_risk") else ""
        lines.append(f"  - {f.path} ({f.language.value}){risk_marker}")
        # Symbol names are safe to expose (identifiers, not content)
        if f.symbols:
            safe_syms = [s.name for s in f.symbols[:3] if s.name.isidentifier()]
            if safe_syms:
                lines.append(f"    Symbols: {', '.join(safe_syms)}")
    return "\n".join(lines)
