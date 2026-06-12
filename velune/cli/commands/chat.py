"""Interactive conversational chat entry point with low-latency streaming."""

from __future__ import annotations

import typer
from rich.console import Console

from velune.cli.context import CLIContext
from velune.cognition.firewall import CognitiveFirewall
from velune.models.specializations import CouncilRole
from velune.repository.schemas import RepositorySnapshot

console = Console()


def chat_command(ctx: typer.Context) -> None:
    """Converses directly with the codebase using a fast, single-model streaming interface."""
    cli_context = ctx.obj
    if not isinstance(cli_context, CLIContext):
        raise typer.BadParameter("CLI context was not properly initialized")

    from velune.core.event_loop import submit
    submit(_chat_command_async(cli_context))


async def _chat_command_async(cli_context: CLIContext) -> None:
    # 1. Access services from DI container
    container = cli_context.container
    lifecycle = container.get("runtime.lifecycle")
    model_registry = container.get("runtime.model_registry")
    model_specialization = container.get("runtime.council_orchestrator").mapper
    repo_cognition = container.get("runtime.repository_cognition")

    # 2. Boot subsystems
    console.print("[bold cyan]⠋[/bold cyan] Bootstrapping Cognitive Operating System kernel...")
    await lifecycle.startup()
    await model_registry.refresh()

    # Onboarding preflight check gate
    from velune.cli.commands.preflight import run_preflight_check
    if not await run_preflight_check(container, console):
        await lifecycle.shutdown()
        return

    # 3. Map Coder role to get fast, single-model access
    roles = model_specialization.map_roles()
    coder_model = roles.get(CouncilRole.CODER)
    if not coder_model:
        from velune.cli.rendering.error_panel import render_error
        from velune.core.errors.catalog import NoModelsAvailableError
        console.print(render_error(NoModelsAvailableError(
            cause_override="No Coder model is assigned or found in the model catalog."
        )))
        await lifecycle.shutdown()
        return

    provider_registry = container.get("runtime.provider_registry")
    provider = provider_registry.get_or_raise(coder_model.provider_id)

    # 4. Ingest and Scan AST Snapshot
    with console.status("[bold magenta]⚡ Scanning codebase AST structure...[/bold magenta]"):
        snapshot = repo_cognition.index()

    firewall = CognitiveFirewall()
    formatted_snap = _format_snapshot_context_safe(snapshot, firewall)

    # 5. Initialize conversation history with codebase context
    messages = [
        {
            "role": "system",
            "content": (
                "You are the Lead Coder for the Velune Reasoning Council, serving in low-latency conversational mode.\n"
                "Your objective is to answer questions, explain code, and assist with natural language tasks concisely and directly.\n"
                "You have access to the user's workspace context details below.\n"
                "Keep responses focused, and do not use verbose pleasantries."
            )
        },
        {
            "role": "system",
            "content": f"User's workspace context summary:\n{formatted_snap}"
        }
    ]

    console.print()
    console.print("[bold green]Velune Chat Mode[/bold green] (type [cyan]!exit[/cyan] to quit, [cyan]!run <task>[/cyan] to escalate to full council)")
    console.print("--------------------------------------------------------------------------------")

    while True:
        try:
            user_input = console.input("[bold green]You:[/bold green] ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Exiting chat session. Goodbye![/yellow]")
            break

        if not user_input:
            continue

        if user_input.lower() in ("!exit", "!quit"):
            console.print("[yellow]Exiting chat session. Goodbye![/yellow]")
            break

        # 6. Escalation handler to full council
        if user_input.startswith("!run "):
            task = user_input[5:].strip()
            if not task:
                console.print("[yellow]Usage: !run <task>  — please specify a task to execute[/yellow]")
                continue
            console.print(f"[bold cyan]Escalating task to full Reasoning Council: '{task}'...[/bold cyan]")

            orchestration_engine = container.get("runtime.orchestration_engine")
            try:
                milestones = []
                async for milestone in orchestration_engine.stream(task):
                    console.print(f"  [bold cyan]•[/bold cyan] {milestone}")
                    milestones.append(milestone)

                run_id = None
                for m in milestones:
                    if hasattr(m, "run_id"):
                        run_id = m.run_id
                        break
                    elif isinstance(m, str) and m.startswith("[") and "]" in m:
                        run_id = m.split("]")[0][1:]
                        break
                state = orchestration_engine.get_state(run_id) if run_id else None
                if state:
                    from velune.orchestration.schemas import ExecutionStatus
                    success = state.status == ExecutionStatus.COMPLETED
                    if success:
                        console.print(f"[bold green]✓ Execution completed successfully: {state.output or 'Done'}[/bold green]")
                    else:
                        from velune.cli.rendering.error_panel import render_unexpected_error
                        console.print(render_unexpected_error(RuntimeError(state.error or "Unknown execution failure")))
                else:
                    console.print("[dim]Council run completed but returned no state.[/dim]")
            except Exception as e:
                from velune.cli.rendering.error_panel import render_error, render_unexpected_error
                from velune.core.errors.catalog import VeluneError
                if isinstance(e, VeluneError):
                    console.print(render_error(e))
                else:
                    console.print(render_unexpected_error(e))
            continue

        # 7. Low-latency, streaming conversational execution
        messages.append({"role": "user", "content": user_input})
        from velune.core.types.inference import InferenceRequest
        request = InferenceRequest(
            model_id=coder_model.model_id,
            messages=messages,
            temperature=0.3,
        )

        console.print("[bold cyan]Velune:[/bold cyan] ", end="")
        full_response_content = []

        try:
            capabilities = provider.get_capabilities()
            supports_streaming = getattr(capabilities, "supports_streaming", False) and hasattr(provider, "stream")

            if supports_streaming:
                try:
                    async for chunk in provider.stream(request):
                        print(chunk.content, end="", flush=True)
                        full_response_content.append(chunk.content)
                    print()
                except KeyboardInterrupt:
                    print()
                    console.print("[yellow]\nGeneration cancelled by user.[/yellow]")
            else:
                response = await provider.infer(request)
                console.print(response.content)
                full_response_content.append(response.content)

            if full_response_content:
                assistant_response = "".join(full_response_content)
                messages.append({"role": "assistant", "content": assistant_response})

        except Exception as e:
            print()
            from velune.cli.rendering.error_panel import render_error, render_unexpected_error
            from velune.core.errors.catalog import VeluneError
            if isinstance(e, VeluneError):
                console.print(render_error(e))
            else:
                console.print(render_unexpected_error(e))

    # 8. Shutdown
    await lifecycle.shutdown()


def _format_snapshot_context_safe(snapshot: RepositorySnapshot, firewall: CognitiveFirewall) -> str:
    """Format snapshot metadata context for query prompt securely."""
    lines = [f"Repository Root: {snapshot.root_path}"]
    lines.append("Codebase Files:")
    for f in snapshot.files[:25]:
        risk_marker = " [⚠ injection-risk]" if f.metadata.get("injection_risk") else ""
        lines.append(f"  - {f.path} ({f.language.value}){risk_marker}")
        if f.symbols:
            safe_syms = [s.name for s in f.symbols[:3] if s.name.isidentifier()]
            if safe_syms:
                lines.append(f"    Symbols: {', '.join(safe_syms)}")
    return "\n".join(lines)
