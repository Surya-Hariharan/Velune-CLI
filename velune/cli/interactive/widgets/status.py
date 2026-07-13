"""Non-interactive progress widget: spinner → terminal ✓ / ✗ frame.

The "validating… → verified" effect. ``WizardController.show_transient()``
already does this, but only *inside* the full-screen wizard chrome, so REPL
slash commands could not reach it — which is why ``/providers`` printed a flat
``Validating...`` line and no result badge.

Unlike every other widget here, this one takes no input: it is driven by the
lifetime of a coroutine, not by keystrokes. It still implements the ``Widget``
contract so it can be hosted by the same machinery, but its ``key_bindings()``
are empty and only Esc/Ctrl-C (owned by ``keys.common_bindings``) can interrupt
it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings

from velune.cli import design
from velune.cli.interactive.widget import Widget

# Braille spinner — single-width in every modern terminal, and degrades to a
# stationary glyph rather than mojibake if the font lacks it.
SPINNER_FRAMES: tuple[str, ...] = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# How long a terminal (✓/✗) frame is held before the widget closes. Long enough
# to read, short enough not to feel like a stall.
HOLD_SECONDS = 0.6

# Spinner advance interval.
TICK_SECONDS = 0.08


@dataclass(kw_only=True)
class StatusWidget(Widget):
    """Renders ``pending`` beside a spinner, then a final ✓/✗ line."""

    pending: str

    _tick: int = field(default=0, init=False)
    # None while running; True/False once the outcome is known.
    _ok: bool | None = field(default=None, init=False)
    _final: str = field(default="", init=False)

    def advance(self) -> None:
        self._tick += 1

    def settle(self, *, ok: bool, message: str) -> None:
        """Switch to the terminal frame. Called once, when the work finishes."""
        self._ok = ok
        self._final = message

    def render(self) -> StyleAndTextTuples:
        if self._ok is None:
            frame = SPINNER_FRAMES[self._tick % len(SPINNER_FRAMES)]
            return [
                (f"fg:{design.ACCENT}", f"\n  {frame} "),
                (f"fg:{design.MUTED}", f"{self.pending}\n"),
            ]

        if self._ok:
            return [
                (f"bold fg:{design.OK}", f"\n  {design.ICON_SUCCESS} "),
                (f"fg:{design.WHITE}", f"{self._final}\n"),
            ]
        return [
            (f"bold fg:{design.DANGER}", f"\n  {design.ICON_ERROR} "),
            (f"fg:{design.WHITE}", f"{self._final}\n"),
        ]

    def key_bindings(self) -> KeyBindings:
        # Nothing to bind — this widget is driven by a coroutine, not the user.
        # Esc / Ctrl-C still work; they belong to keys.common_bindings().
        return KeyBindings()

    def footer_hint(self) -> str:
        return "" if self._ok is not None else "Esc cancel"
