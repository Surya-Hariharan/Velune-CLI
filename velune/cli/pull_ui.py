"""Interactive model browser TUI for /pull — select a model to download."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Window
from prompt_toolkit.layout.controls import FormattedTextControl

from velune.providers.ollama_manager import RECOMMENDED_MODELS

if TYPE_CHECKING:
    from rich.console import Console

_SKILL_STYLES: dict[str, str] = {
    "coding": "fg:ansigreen",
    "reasoning": "fg:ansimagenta",
    "embedding": "fg:ansicyan",
    "general": "fg:ansibrightblack",
}


async def run_pull_ui(
    local_models: list[str],
    hardware_ram_gb: float,
    console: Console,
) -> str | None:
    """Show the interactive model browser and return the chosen model_id, or None."""

    selected_idx = [0]
    result: list[str | None] = [None]

    def _fits(model: dict) -> bool:
        try:
            needed = float(model["ram_needed"].replace(" GB", "").strip())
            return hardware_ram_gb >= needed
        except Exception:
            return True

    def render_list() -> FormattedText:
        lines: list[tuple[str, str]] = []
        lines.append(("bold", "  Available models to pull\n"))
        lines.append(("fg:ansibrightblack", "  ↑↓ navigate · Enter pull · Esc cancel\n\n"))
        lines.append(("fg:ansibrightblack", f"  Your RAM: {hardware_ram_gb:.0f} GB\n\n"))

        for i, model in enumerate(RECOMMENDED_MODELS):
            is_active = i == selected_idx[0]
            prefix = "❯ " if is_active else "  "
            row_style = "bold fg:cyan" if is_active else ""
            model_id = model["model_id"]
            is_local = any(
                m == model_id or m.split(":")[0] == model_id.split(":")[0] and m == model_id
                for m in local_models
            )
            fits = _fits(model)
            skill = model.get("skill", "general")
            skill_style = _SKILL_STYLES.get(skill, "fg:ansibrightblack")

            if is_local:
                status = "  installed"
                status_style = "fg:ansigreen"
            elif not fits:
                status = "  needs more RAM"
                status_style = "fg:ansibrightblack"
            else:
                status = ""
                status_style = ""

            lines.append(
                (
                    row_style,
                    f"  {prefix}{model_id:<34} {model['size_gb']:4.1f} GB  ",
                )
            )
            lines.append((skill_style, f"[{skill}]"))
            if status:
                lines.append((status_style, status))
            lines.append(("", "\n"))

            if is_active:
                lines.append(("fg:ansibrightblack", f"         {model['description']}\n"))
                lines.append(
                    ("fg:ansibrightblack", f"         RAM needed: {model['ram_needed']}\n")
                )

        return FormattedText(lines)

    kb = KeyBindings()

    @kb.add("up")
    def _up(event) -> None:
        selected_idx[0] = (selected_idx[0] - 1) % len(RECOMMENDED_MODELS)

    @kb.add("down")
    def _down(event) -> None:
        selected_idx[0] = (selected_idx[0] + 1) % len(RECOMMENDED_MODELS)

    @kb.add("enter")
    def _select(event) -> None:
        result[0] = RECOMMENDED_MODELS[selected_idx[0]]["model_id"]
        event.app.exit()

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event) -> None:
        event.app.exit()

    app = Application(
        layout=Layout(
            Window(
                content=FormattedTextControl(render_list, focusable=True),
            )
        ),
        key_bindings=kb,
        full_screen=False,
        mouse_support=False,
    )
    await app.run_async()
    return result[0]
