"""Interactive two-stage TUI for assigning models to council agent roles."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl

from velune.orchestration.role_assignments import (
    COUNCIL_ROLES,
    ROLE_DESCRIPTIONS,
    CouncilRoleMap,
)

if TYPE_CHECKING:
    from rich.console import Console

    from velune.core.types.model import ModelDescriptor

# Sentinel to distinguish "user pressed Escape" from "user selected clear"
_CANCELLED = object()


async def run_councilmodel_ui(
    role_map: CouncilRoleMap,
    available_models: list[ModelDescriptor],
    console: Console,
) -> CouncilRoleMap | None:
    """Two-stage interactive UI: select a role, then select a model for it.

    Returns the updated role_map, or None if cancelled at the role-select stage.
    """

    # ── Stage 1: Role selection ────────────────────────────────────────
    disabled_roles = {"architect", "security", "challenger", "synthesizer"}
    active_indices = [i for i, r in enumerate(COUNCIL_ROLES) if r not in disabled_roles]
    selected_role_idx = [active_indices[0] if active_indices else 0]
    role_result: list[str | None] = [None]

    def render_role_list() -> FormattedText:
        lines: list[tuple[str, str]] = []
        lines.append(("bold", "  Assign model to council role\n"))
        lines.append(("fg:ansibrightblack", "  ↑↓ navigate · Enter select · Esc cancel\n\n"))
        for i, role in enumerate(COUNCIL_ROLES):
            is_active = i == selected_role_idx[0]
            prefix = "❯ " if is_active else "  "

            if role in disabled_roles:
                row_style = "fg:ansibrightblack"
                tag = " [disabled]"
                prefix = "  "
                desc = ROLE_DESCRIPTIONS.get(role, "")
                lines.append((row_style, f"  {prefix}{role:<14} {desc}{tag}\n"))
            else:
                row_style = "bold fg:cyan" if is_active else ""
                desc = ROLE_DESCRIPTIONS.get(role, "")
                lines.append((row_style, f"  {prefix}{role:<14} {desc}\n"))
                current = role_map.get(role)
                if current:
                    lines.append(
                        ("fg:ansibrightblack", f"               currently: {current.model_id}\n")
                    )
        return FormattedText(lines)

    kb1 = KeyBindings()

    @kb1.add("up")
    def _up(event) -> None:
        curr = selected_role_idx[0]
        while True:
            curr = (curr - 1) % len(COUNCIL_ROLES)
            if curr in active_indices:
                selected_role_idx[0] = curr
                break

    @kb1.add("down")
    def _down(event) -> None:
        curr = selected_role_idx[0]
        while True:
            curr = (curr + 1) % len(COUNCIL_ROLES)
            if curr in active_indices:
                selected_role_idx[0] = curr
                break

    @kb1.add("enter")
    def _select_role(event) -> None:
        role_result[0] = COUNCIL_ROLES[selected_role_idx[0]]
        event.app.exit()

    @kb1.add("escape")
    @kb1.add("c-c")
    def _cancel_role(event) -> None:
        event.app.exit()  # role_result stays None

    app1 = Application(
        layout=Layout(
            Window(
                content=FormattedTextControl(render_role_list, focusable=True),
            )
        ),
        key_bindings=kb1,
        full_screen=False,
        mouse_support=False,
    )
    await app1.run_async()

    selected_role = role_result[0]
    if selected_role is None:
        return None  # cancelled at role stage

    # ── Stage 2: Model selection for chosen role ───────────────────────
    # First entry is None = "clear assignment"
    model_options: list[ModelDescriptor | None] = [None] + list(available_models)
    selected_model_idx = [0]
    model_result: list[object] = [_CANCELLED]

    def render_model_list() -> FormattedText:
        lines: list[tuple[str, str]] = []
        lines.append(("bold", f"  Select model for [{selected_role}]\n"))
        lines.append(("fg:ansibrightblack", "  ↑↓ navigate · Enter select · Esc back\n\n"))

        for i, model in enumerate(model_options):
            is_active = i == selected_model_idx[0]
            prefix = "❯ " if is_active else "  "
            row_style = "bold fg:cyan" if is_active else ""

            if model is None:
                lines.append((row_style, f"  {prefix}(clear — use default routing)\n"))
                continue

            current = role_map.get(selected_role)
            is_current = current is not None and current.model_id == model.model_id
            current_marker = " ← current" if is_current else ""
            local_cloud = "local" if model.is_local else "cloud"
            cost = getattr(model, "cost_per_1k_tokens", None)
            free_str = " free" if cost == 0.0 else ""
            lines.append(
                (
                    row_style,
                    f"  {prefix}{model.model_id:<42}"
                    f" [{local_cloud}{free_str} · {model.speed_tier}]"
                    f"{current_marker}\n",
                )
            )
        return FormattedText(lines)

    kb2 = KeyBindings()

    @kb2.add("up")
    def _up2(event) -> None:
        selected_model_idx[0] = (selected_model_idx[0] - 1) % len(model_options)

    @kb2.add("down")
    def _down2(event) -> None:
        selected_model_idx[0] = (selected_model_idx[0] + 1) % len(model_options)

    @kb2.add("enter")
    def _select_model(event) -> None:
        model_result[0] = model_options[selected_model_idx[0]]
        event.app.exit()

    @kb2.add("escape")
    @kb2.add("c-c")
    def _cancel_model(event) -> None:
        event.app.exit()  # model_result stays _CANCELLED

    app2 = Application(
        layout=Layout(
            Window(
                content=FormattedTextControl(render_model_list, focusable=True),
            )
        ),
        key_bindings=kb2,
        full_screen=False,
        mouse_support=False,
    )
    await app2.run_async()

    if model_result[0] is _CANCELLED:
        return role_map  # user backed out — return map unchanged

    chosen_model = model_result[0]
    if chosen_model is None:
        role_map.clear_role(selected_role)
        console.print(f"[yellow]Cleared assignment for [{selected_role}][/yellow]")
    else:
        role_map.assign(selected_role, chosen_model.model_id, chosen_model.provider_id)
        console.print(
            f"[green][{selected_role}][/green] → "
            f"[cyan]{chosen_model.model_id}[/cyan] "
            f"[dim]({chosen_model.provider_id})[/dim]"
        )

    return role_map
