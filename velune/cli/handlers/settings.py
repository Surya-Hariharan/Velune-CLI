"""Settings slash command handlers: /config /hooks /approve /doctor /sandbox."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.settings")


async def cmd_config(repl: VeluneREPL, args: str) -> None:
    """Show current system configuration settings."""
    import logging as _logging
    from pathlib import Path

    from rich.panel import Panel
    from rich.table import Table

    config = repl.runtime.config

    table = Table(show_header=True, border_style="cyan")
    table.add_column("Setting", style="bold yellow")
    table.add_column("Value", style="green")

    table.add_row("Config Path", str(repl.runtime.config_path or "default (memory)"))
    table.add_row("Workspace Root", str(repl.runtime.workspace or Path.cwd()))
    verbose = _logging.getLogger("velune").getEffectiveLevel() <= _logging.DEBUG
    table.add_row("Log Level", "DEBUG" if verbose else "INFO")

    if hasattr(config, "model_dump"):
        dump = config.model_dump()
    elif hasattr(config, "dict"):
        dump = getattr(config, "dict")()
    else:
        dump = {}

    def flatten_dict(d: dict, prefix: str = "") -> None:
        for k, v in d.items():
            name = f"{prefix}{k}"
            if isinstance(v, dict):
                flatten_dict(v, prefix=f"{name}.")
            else:
                table.add_row(name, str(v))

    flatten_dict(dump)

    repl.console.print(
        Panel(
            table,
            title="[bold white]Velune System Configuration[/bold white]",
            border_style="cyan",
            padding=(1, 2),
        )
    )


async def cmd_hooks(repl: VeluneREPL, args: str) -> None:
    """List active hook bindings from project + user config."""
    from rich.table import Table

    rows = repl._hook_dispatcher.summary()
    if not rows:
        repl.console.print(
            "[dim]No hooks configured. "
            "Create [bold].velune/hooks.json[/bold] or [bold]~/.velune/hooks.json[/bold] to add lifecycle hooks.[/dim]"
        )
        return

    table = Table(
        show_header=True,
        border_style="dim",
        padding=(0, 1),
        header_style="bold cyan",
        title="[bold cyan]Lifecycle Hooks[/bold cyan]",
    )
    table.add_column("Event", style="cyan", width=20)
    table.add_column("Matcher", style="dim white", width=14)
    table.add_column("Command", width=34)
    table.add_column("Timeout", justify="right", width=9)
    table.add_column("If Condition", style="dim yellow")

    for row in rows:
        table.add_row(
            row.get("event", ""),
            row.get("matcher", "*") or "*",
            row.get("command", ""),
            f"{row.get('timeout', 10)}s",
            row.get("if", "") or "—",
        )

    repl.console.print(table)
    repl.console.print(
        f"\n[dim]{len(rows)} hook(s) loaded. "
        "Use [bold]/hooks[/bold] after editing hooks.json to see updates (cache reloads automatically).[/dim]"
    )


async def cmd_approve(repl: VeluneREPL, args: str) -> None:
    """Set the tool/command approval mode for this session."""
    from velune.tools.safety import ApprovalMode

    sub = args.strip().lower()
    if not sub:
        modes = ", ".join(m.value for m in ApprovalMode)
        repl.console.print(
            f"[cyan]Current approval mode:[/cyan] [bold]{repl._approval_mode.value}[/bold]\n"
            f"[dim]Usage: /approve [{modes}][/dim]\n"
            f"\n"
            f"  [bold]safe[/bold]   — known read-only commands run without prompting\n"
            f"  [bold]ask[/bold]    — all tool/shell calls require confirmation  [dim](default)[/dim]\n"
            f"  [bold]block[/bold]  — all shell tool calls are rejected"
        )
        return

    try:
        new_mode = ApprovalMode(sub)
    except ValueError:
        modes = " | ".join(m.value for m in ApprovalMode)
        repl.console.print(f"[red]Unknown mode: {sub!r}[/red]  [dim]Choose: {modes}[/dim]")
        return

    repl._approval_mode = new_mode
    style = {"safe": "green", "ask": "yellow", "block": "red"}.get(new_mode.value, "white")
    repl.console.print(
        f"[{style}]Approval mode set to:[/{style}] [bold]{new_mode.value}[/bold]"
    )


async def cmd_doctor(repl: VeluneREPL, args: str) -> None:
    from velune.cli.commands.doctor import (
        _check_anthropic_api_key,
        _check_config,
        _check_core_dependencies,
        _check_git,
        _check_gpu,
        _check_groq,
        _check_lm_studio,
        _check_model_benchmarks,
        _check_ollama_connectivity,
        _check_ollama_models,
        _check_openai_api_key,
        _check_python_version,
        _check_qdrant,
        _check_sqlite,
        _check_treesitter,
        _check_velune_dir,
        _check_vram,
        _render_results,
    )

    checks = [
        _check_python_version,
        _check_core_dependencies,
        _check_ollama_connectivity,
        _check_ollama_models,
        _check_lm_studio,
        _check_openai_api_key,
        _check_anthropic_api_key,
        _check_groq,
        _check_velune_dir,
        _check_sqlite,
        _check_qdrant,
        _check_config,
        _check_treesitter,
        _check_git,
        _check_gpu,
        _check_vram,
        _check_model_benchmarks,
    ]
    results = []
    with repl.console.status("[cyan]Running health checks...[/cyan]"):
        for check_fn in checks:
            try:
                results.append(check_fn())
            except Exception as e:
                results.append(
                    {
                        "name": check_fn.__name__.replace("_check_", "")
                        .replace("_", " ")
                        .title(),
                        "status": "error",
                        "message": str(e),
                    }
                )
    _render_results(results)
    failures = sum(1 for r in results if r["status"] == "fail")
    if failures:
        repl.console.print(
            f"[red]{failures} check(s) failed.[/red]  "
            "[dim]Run [cyan]velune doctor --fix[/cyan] to attempt automatic fixes.[/dim]"
        )
    else:
        repl.console.print("[green]All checks passed.[/green]")


async def cmd_sandbox(repl: VeluneREPL, args: str) -> None:
    """Show current sandbox type and status, or start Docker sandbox."""
    from pathlib import Path

    sub = args.strip().lower()

    workspace_raw = repl.container.get("runtime.workspace")
    workspace = Path(workspace_raw) if workspace_raw else Path.cwd()

    if sub in ("docker", "start"):
        from velune.execution.docker_sandbox import DockerSandbox, DockerUnavailableError

        try:
            sb = DockerSandbox.for_workspace(workspace)
            with repl.console.status("[cyan]Starting Docker sandbox…[/cyan]"):
                sb.start()
            repl.console.print(
                f"[green]Docker sandbox started[/green]\n"
                f"  Container: [bold]{sb.session_id}[/bold]\n"
                f"  Image:     [dim]{sb.image}[/dim]\n"
                f"  Workspace: [dim]{workspace} → /workspace[/dim]\n\n"
                f"[dim]This sandbox is standalone. To route agent execution through Docker,\n"
                f"set [bold]execution.docker_sandbox = true[/bold] in [bold]velune.toml[/bold].[/dim]"
            )
        except DockerUnavailableError as exc:
            repl.console.print(
                f"[red]Docker unavailable:[/red] {exc}\n"
                "[dim]Install Docker Desktop and ensure the daemon is running.[/dim]"
            )
        except Exception as exc:
            repl.console.print(f"[red]Sandbox start failed:[/red] {exc}")
        return

    # Default: show status info
    try:
        from velune.execution.docker_sandbox import DockerSandbox

        test_sb = DockerSandbox.for_workspace(workspace)
        test_client = test_sb._get_docker_client()
        docker_info = test_client.version()
        docker_version = docker_info.get("Version", "unknown")
        docker_ok = True
    except Exception:
        docker_version = "unavailable"
        docker_ok = False

    try:
        from velune.kernel.config import ConfigLoader

        cfg = ConfigLoader(workspace / "velune.toml").load()
        docker_configured = getattr(getattr(cfg, "execution", None), "docker_sandbox", False)
        docker_image = getattr(
            getattr(cfg, "execution", None), "docker_image", "python:3.12-slim"
        )
    except Exception:
        docker_configured = False
        docker_image = "python:3.12-slim"

    active = "Docker" if docker_configured and docker_ok else "Subprocess"
    docker_status = (
        f"[green]available v{docker_version}[/green]" if docker_ok else "[red]unavailable[/red]"
    )

    repl.console.print(
        f"\n[bold cyan]Sandbox Status[/bold cyan]\n"
        f"  Active mode:   [bold]{active}[/bold]\n"
        f"  Docker daemon: {docker_status}\n"
        f"  Docker image:  [dim]{docker_image}[/dim]\n"
        f"  Configured:    [bold]{'docker' if docker_configured else 'subprocess'}[/bold] "
        f"[dim](execution.docker_sandbox in velune.toml)[/dim]\n\n"
        f"[dim]Run [bold]/sandbox docker[/bold] to test-start a Docker sandbox.[/dim]\n"
        f"[dim]Set [bold]execution.docker_sandbox = true[/bold] in velune.toml to route all agent execution through Docker.[/dim]"
    )
