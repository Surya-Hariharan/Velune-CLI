"""Yes/No confirmation widget — arrow-key selectable, with y/n shortcuts.

Replaces the ``rich.prompt.Confirm.ask`` calls scattered through the wizards
(workspace registration, "replace existing key?", "use this model?") with a
widget that shares the same render/key_bindings/footer_hint shape as every
other interactive primitive.
"""

from __future__ import annotations

from dataclasses import dataclass

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings

from velune.cli import design
from velune.cli.interactive.widget import Widget


@dataclass(kw_only=True)
class ConfirmWidget(Widget):
    question: str
    hint: str = ""
    default: bool = True

    _choice: bool = False

    def __post_init__(self) -> None:
        self._choice = self.default

    def render(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = [
            (f"bold fg:{design.WHITE}", f"  {self.question}\n"),
        ]
        if self.hint:
            lines.append((f"fg:{design.MUTED}", f"  {self.hint}\n"))
        lines.append(("", "\n"))

        yes_sel = self._choice is True
        no_sel = self._choice is False

        yes_style = f"bold fg:{design.ACCENT}" if yes_sel else f"fg:{design.WHITE}"
        no_style = f"bold fg:{design.ACCENT}" if no_sel else f"fg:{design.WHITE}"

        lines.append((yes_style, f"  {'❯ ' if yes_sel else '  '}Yes"))
        lines.append(("", "    "))
        lines.append((no_style, f"{'❯ ' if no_sel else '  '}No\n"))
        return lines

    def key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("left")
        @kb.add("up")
        @kb.add("s-tab")
        def _left(event) -> None:
            self._choice = True

        @kb.add("right")
        @kb.add("down")
        @kb.add("tab")
        def _right(event) -> None:
            self._choice = False

        @kb.add("y")
        @kb.add("Y")
        def _yes(event) -> None:
            self._choice = True
            self.on_submit(True)

        @kb.add("n")
        @kb.add("N")
        def _no(event) -> None:
            self._choice = False
            self.on_submit(False)

        @kb.add("enter")
        def _enter(event) -> None:
            self.on_submit(self._choice)

        return kb

    def footer_hint(self) -> str:
        return "←→ choose  ·  y/n  ·  Enter confirm  ·  Esc back"
