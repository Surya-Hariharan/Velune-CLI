"""Interactive model browser widget for /pull — select an Ollama model to download."""

from __future__ import annotations

from dataclasses import dataclass, field

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys

from velune.cli import design
from velune.cli.interactive.widget import Widget
from velune.providers.ollama_manager import RECOMMENDED_MODELS

_SKILL_COLORS: dict[str, str] = {
    "coding": design.OK,
    "reasoning": design.ACCENT,
    "embedding": design.INFO,
    "general": design.MUTED,
}


@dataclass(kw_only=True)
class ModelPullWidget(Widget[str]):
    """Browse ``RECOMMENDED_MODELS``; submits the chosen model id."""

    local_models: list[str]
    hardware_ram_gb: float

    _index: int = field(default=0, init=False)

    def _fits(self, model: dict) -> bool:
        try:
            needed = float(model["ram_needed"].replace(" GB", "").strip())
            return self.hardware_ram_gb >= needed
        except Exception:
            return True

    def _is_local(self, model_id: str) -> bool:
        return any(m == model_id for m in self.local_models)

    def move(self, delta: int) -> None:
        if not RECOMMENDED_MODELS:
            return
        self._index = (self._index + delta) % len(RECOMMENDED_MODELS)

    def submit(self) -> None:
        if not RECOMMENDED_MODELS:
            return
        self.on_submit(RECOMMENDED_MODELS[self._index]["model_id"])

    def render(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = [
            (f"bold fg:{design.ACCENT}", "  Available models to pull\n"),
            (f"fg:{design.MUTED}", f"  Your RAM: {self.hardware_ram_gb:.0f} GB\n\n"),
        ]

        for i, model in enumerate(RECOMMENDED_MODELS):
            is_active = i == self._index
            prefix = "❯ " if is_active else "  "
            row_style = f"bold fg:{design.ACCENT}" if is_active else f"fg:{design.WHITE}"
            model_id = model["model_id"]
            fits = self._fits(model)
            skill = model.get("skill", "general")
            skill_color = _SKILL_COLORS.get(skill, design.MUTED)

            if self._is_local(model_id):
                status, status_color = "  installed", design.OK
            elif not fits:
                status, status_color = "  needs more RAM", design.MUTED
            else:
                status, status_color = "", ""

            lines.append((row_style, f"  {prefix}{model_id:<34} {model['size_gb']:4.1f} GB  "))
            lines.append((f"fg:{skill_color}", f"[{skill}]"))
            if status:
                lines.append((f"fg:{status_color}", status))
            lines.append(("", "\n"))

            if is_active:
                lines.append((f"fg:{design.MUTED}", f"         {model['description']}\n"))
                lines.append(
                    (f"fg:{design.MUTED}", f"         RAM needed: {model['ram_needed']}\n")
                )

        return lines

    def key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("up")
        def _up(event) -> None:
            self.move(-1)

        @kb.add("down")
        def _down(event) -> None:
            self.move(1)

        @kb.add(Keys.ScrollUp, eager=True)
        def _scroll_up(event) -> None:
            self.move(-1)

        @kb.add(Keys.ScrollDown, eager=True)
        def _scroll_down(event) -> None:
            self.move(1)

        @kb.add("enter")
        def _enter(event) -> None:
            self.submit()

        return kb

    def footer_hint(self) -> str:
        return "↑↓ navigate  ·  Enter pull  ·  Esc cancel"
