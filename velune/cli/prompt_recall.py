"""Edit-and-resend: cycling the prompt box through previously sent messages.

Extracted out of the REPL's Up/Down key bindings (see
``VeluneREPL._build_fullscreen_ui``) so the recall state machine is
unit-testable without a running prompt_toolkit ``Application``.
"""

from __future__ import annotations


class PromptRecallState:
    """Tracks which previously-sent prompt, if any, is loaded for editing.

    ``index`` is ``None`` when not recalling; otherwise an index into the
    caller-supplied prompt list (oldest first, same order
    ``VeluneREPL._conversation`` accumulates in). Editing the recalled text,
    or a new turn being submitted since (which changes what ``prompts[index]``
    would even mean), is detected by :meth:`still_recalling` comparing the
    live buffer text against the snapshot — no explicit reset hook is needed
    for either case; the next Up simply starts fresh from the newest prompt.
    """

    def __init__(self) -> None:
        self.index: int | None = None

    def still_recalling(self, current_text: str, prompts: list[str]) -> bool:
        idx = self.index
        return idx is not None and 0 <= idx < len(prompts) and current_text == prompts[idx]

    def recall_up(self, current_text: str, prompts: list[str]) -> str | None:
        """Text to load on Up, or None to fall back to normal cursor/history behavior."""
        recalling = self.still_recalling(current_text, prompts)
        if not (recalling or not current_text):
            return None
        if not prompts:
            return None
        idx = self.index
        new_idx = (idx - 1) if (recalling and idx > 0) else (len(prompts) - 1)
        self.index = new_idx
        return prompts[new_idx]

    def recall_down(self, current_text: str, prompts: list[str]) -> str | None:
        """Text to load on Down (possibly ""), or None to fall back."""
        if not self.still_recalling(current_text, prompts):
            return None
        idx = self.index
        assert idx is not None
        if idx < len(prompts) - 1:
            self.index = idx + 1
            return prompts[idx + 1]
        self.index = None
        return ""
