"""Council model assignment slash command handlers: /roles."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from velune.cli.repl import VeluneREPL

_log = logging.getLogger("velune.cli.handlers.councilmodel")


def apply_role_overrides_to_orchestrator(repl: VeluneREPL) -> None:
    """Push current role assignments into the orchestrator's mapper overrides."""
    try:
        orchestrator = repl.container.get("runtime.council_orchestrator")
        if not orchestrator or not hasattr(orchestrator, "mapper"):
            return
        from velune.models.specializations import CouncilRole

        orchestrator.mapper.overrides.clear()
        for role_str, assignment in repl._role_map.assignments.items():
            try:
                orchestrator.mapper.overrides[CouncilRole(role_str)] = assignment.model_id
            except ValueError:
                pass
        if hasattr(orchestrator, "agent_factory"):
            orchestrator.agent_factory.clear_cache()
    except Exception:
        pass


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

    from velune.orchestration.role_assignments import COUNCIL_ROLES, ROLE_DESCRIPTIONS

    table = Table(border_style="dim", padding=(0, 1))
    table.add_column("Role", style="cyan", width=14)
    table.add_column("Assigned Model", style="white")
    table.add_column("Provider", style="dim")
    table.add_column("Description", style="dim")
    for role in COUNCIL_ROLES:
        assignment = repl._role_map.get(role)
        model_str = assignment.model_id if assignment else "[dim]auto-routed[/dim]"
        provider_str = assignment.provider_id if assignment else "—"
        table.add_row(
            role,
            model_str,
            provider_str,
            ROLE_DESCRIPTIONS.get(role, "")[:45],
        )
    repl.console.print(table)
    if not repl._role_map.assignments:
        repl.console.print("[dim]No custom assignments. Use /roles to assign.[/dim]")
