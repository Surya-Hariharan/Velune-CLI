"""Interactive two-stage flow for assigning models to council agent roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from velune.cli.interactive import BACK, CANCEL, Option, single_select
from velune.orchestration.role_assignments import (
    EFFECTIVE_ROLES,
    ROLE_DESCRIPTIONS,
    CouncilRoleMap,
)

if TYPE_CHECKING:
    from rich.console import Console

    from velune.core.types.model import ModelDescriptor

_CLEAR = "\x00clear"


async def run_councilmodel_ui(
    role_map: CouncilRoleMap,
    available_models: list[ModelDescriptor],
    console: Console,
) -> CouncilRoleMap | None:
    """Two-stage interactive flow: select a role, then select a model for it.

    Returns the updated role_map, or None if cancelled at the role-select stage.
    """
    # ── Stage 1: Role selection ────────────────────────────────────────
    role_options = []
    for role in EFFECTIVE_ROLES:
        desc = ROLE_DESCRIPTIONS.get(role, "")
        current = role_map.get(role)
        meta = f"{desc}   currently: {current.model_id}" if current else desc
        role_options.append(Option(id=role, label=role, meta=meta))

    selected_role = await single_select("Assign model to council role", role_options)
    if selected_role in (BACK, CANCEL):
        return None  # cancelled at role stage

    # ── Stage 2: Model selection for chosen role ───────────────────────
    # Options are keyed by index (not model_id) so a duplicate model_id
    # across two providers can't resolve to the wrong ModelDescriptor.
    current = role_map.get(selected_role)
    model_options = [Option(id=_CLEAR, label="(clear — use default routing)")]
    for i, model in enumerate(available_models):
        is_current = current is not None and current.model_id == model.model_id
        local_cloud = "local" if model.is_local else "cloud"
        cost = getattr(model, "cost_per_1k_tokens", None)
        free_str = " free" if cost == 0.0 else ""
        model_options.append(
            Option(
                id=str(i),
                label=model.model_id,
                meta=f"[{local_cloud}{free_str} · {model.speed_tier}]",
                badge="current" if is_current else None,
            )
        )

    chosen = await single_select(f"Select model for [{selected_role}]", model_options)
    if chosen in (BACK, CANCEL):
        return role_map  # user backed out — return map unchanged

    if chosen == _CLEAR:
        role_map.clear_role(selected_role)
        console.print(f"[yellow]Cleared assignment for [{selected_role}][/yellow]")
        return role_map

    chosen_model = available_models[int(chosen)]
    role_map.assign(selected_role, chosen_model.model_id, chosen_model.provider_id)
    console.print(
        f"[green][{selected_role}][/green] → "
        f"[cyan]{chosen_model.model_id}[/cyan] "
        f"[dim]({chosen_model.provider_id})[/dim]"
    )
    return role_map
