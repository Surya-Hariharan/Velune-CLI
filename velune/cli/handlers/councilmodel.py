"""Council model assignment slash command handlers: /roles."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL
    from velune.orchestration.role_assignments import RoleOverrideReport

_log = logging.getLogger("velune.cli.handlers.councilmodel")


def apply_role_overrides_to_orchestrator(repl: VeluneREPL) -> RoleOverrideReport | None:
    """Push current role assignments into the orchestrator's mapper overrides.

    Returns what was applied and which saved entries have no effect (and says so once
    per session) instead of dropping them silently.
    """
    from velune.orchestration.role_assignments import apply_role_map

    try:
        orchestrator = repl.container.get("runtime.council_orchestrator")
        if not orchestrator or not hasattr(orchestrator, "mapper"):
            return None
        report: RoleOverrideReport = apply_role_map(orchestrator.mapper, repl._role_map)
        if hasattr(orchestrator, "agent_factory"):
            orchestrator.agent_factory.clear_cache()
    except Exception as exc:
        _log.warning("Could not apply council role assignments: %s", exc)
        return None
    if report.ignored and not getattr(repl, "_role_warning_shown", False):
        console = getattr(repl, "console", None)
        if console is not None:
            console.print(
                "[yellow]Saved council role(s) with no effect, ignored:[/yellow] "
                + ", ".join(sorted(report.ignored))
                + " [dim](see /roles show)[/dim]"
            )
        try:
            repl._role_warning_shown = True
        except Exception:
            pass
    return report


async def cmd_councilmodel(repl: VeluneREPL, args: str) -> None:
    sub = args.strip().lower()
    if sub == "show":
        await cmd_councilmodel_show(repl)
        return
    if sub == "reset":
        repl._role_map.clear_all()
        repl._role_map.save(repl._assignments_path)
        apply_role_overrides_to_orchestrator(repl)
        repl.console.print("[yellow]All council role assignments cleared.[/yellow]")
        return

    model_registry = repl.container.get("runtime.model_registry")
    provider_registry = repl.container.get("runtime.provider_registry")
    available = [
        m
        for m in model_registry.list_all()
        if m.is_local or provider_registry.check_provider_available(m.provider_id)
    ]
    if not available:
        from velune.cli.rendering.error_panel import render_error
        from velune.core.errors.catalog import NoModelsAvailableError

        repl.console.print(render_error(NoModelsAvailableError()))
        return

    from velune.cli.councilmodel_ui import run_councilmodel_ui

    updated = await run_councilmodel_ui(repl._role_map, available, repl.console)
    if updated is not None:
        repl._role_map = updated
        repl._role_map.save(repl._assignments_path)
        apply_role_overrides_to_orchestrator(repl)


async def cmd_councilmodel_show(repl: VeluneREPL) -> None:
    from rich.table import Table
    from rich.text import Text

    from velune.orchestration.role_assignments import EFFECTIVE_ROLES, ROLE_DESCRIPTIONS

    model_registry = None
    try:
        model_registry = repl.container.get("runtime.model_registry")
    except Exception:
        pass

    table = Table(border_style="dim", padding=(0, 1))
    table.add_column("Role", style="cyan", width=14)
    table.add_column("Assigned Model", style="white")
    table.add_column("Provider", style="dim")
    table.add_column("Status", style="dim")
    table.add_column("Description", style="dim")
    for role in EFFECTIVE_ROLES:
        assignment = repl._role_map.get(role)
        status = ""
        if assignment is None:
            model_str, provider_str = "[dim]auto-routed[/dim]", "—"
        else:
            model_str, provider_str = Text(assignment.model_id), assignment.provider_id
            if model_registry is not None:
                known = assignment.provider_id not in ("", "unknown")
                found = model_registry.get(
                    assignment.model_id, assignment.provider_id if known else None
                )
                status = "in use" if found else "unavailable - auto-routed"
        table.add_row(role, model_str, provider_str, status, ROLE_DESCRIPTIONS.get(role, "")[:45])
    repl.console.print(table)

    ignored = repl._role_map.ignored_notes()
    if ignored:
        repl.console.print("[yellow]Ignored (no effect):[/yellow]")
        for role, note in ignored.items():
            repl.console.print(Text(f"  {role}: {note}", style="dim"))
        repl.console.print(
            "[dim]/roles reset clears these along with every other assignment.[/dim]"
        )
    if not repl._role_map.assignments:
        repl.console.print("[dim]No custom assignments. Use /roles to assign.[/dim]")
