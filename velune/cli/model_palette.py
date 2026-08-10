"""Floating model palette for the interactive REPL, opened by typing ``/model``.

Deliberately the command palette's twin: same float geometry, same two-pane
frame, same key contract. The one visible difference is hue — cyan-blue instead
of indigo — because the two occupy the *same* screen rectangle and swap the
instant ``/model`` completes. Without a colour change that swap reads as the
command list mysteriously re-sorting itself; with one it reads as a different
surface taking over.

Selection is arrow-driven only. Enter submits ``/model use <id>`` through the
normal buffer, so switching models here goes down the exact same code path as
typing that command by hand (persistence, recents, default-provider write) —
this module never activates a model itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame

from velune.cli import design

if TYPE_CHECKING:
    from prompt_toolkit.shortcuts import PromptSession

    from velune.core.types.model import ModelDescriptor

#: Command heads that open this palette. Longest first so ``/models`` is not
#: mis-parsed as ``/model`` plus a stray "s".
#:
#: ``/models`` is included because it is the *listing* command, and the palette
#: is a better listing than the one it replaces: submitting ``/models`` with an
#: empty catalog used to print a red "No models available" error panel, which
#: is a dead end — the palette shows the same emptiness with the commands that
#: fix it, and swallows Enter so the line never runs as a prompt.
_TRIGGERS = ("/models", "/model")

#: ``/model`` is a command group, not just a model picker. When the first word
#: after it is one of these, the user is driving a subcommand and the palette
#: must stay shut — otherwise typing ``/model use gpt-4o`` would pop a picker
#: over the command being typed and Enter would submit the wrong thing.
#: ``/models`` takes no subcommands, so this only applies to ``/model``.
_SUBCOMMANDS = frozenset(
    {"discover", "connect", "use", "list", "status", "remove", "locate", "locations"}
)

_VISIBLE_ROWS = 9


@dataclass(frozen=True, slots=True)
class ModelMatch:
    model: Any
    #: Display/sort group — "Local" or "Cloud".
    group: str


def connected_models(container: Any) -> list[ModelDescriptor]:
    """Models usable *right now*: every local model, plus cloud models whose
    provider has a working credential.

    Returns ``[]`` rather than raising when the registries are missing (early
    startup, degraded runtime) — an empty palette with a helpful message is a
    better failure mode than a traceback over the prompt.
    """
    try:
        registry = container.get("runtime.model_registry")
        provider_registry = container.get("runtime.provider_registry")
        all_models = registry.list_all()
    except Exception:
        return []
    try:
        connected = [
            m
            for m in all_models
            if m.is_local or provider_registry.check_provider_available(m.provider_id)
        ]
    except Exception:
        return []
    connected.sort(key=lambda m: (not m.is_local, m.provider_id, m.model_id))
    return connected


def has_any_registered_model(container: Any) -> bool:
    """Whether the catalog knows about any model at all, connected or not.

    Distinguishes "you have models but nothing is reachable" (start Ollama)
    from "you have nothing configured" (add a key / pull a model), which need
    different advice.
    """
    try:
        return bool(container.get("runtime.model_registry").list_all())
    except Exception:
        return False


def empty_state_guidance(has_registered_models: bool) -> tuple[str, str, list[tuple[str, str]]]:
    """``(headline, detail, [(label, command), ...])`` for "there is nothing to pick".

    Single-sourced here so the palette and the ``/model`` / ``/models`` command
    handlers tell the user the same thing — they previously disagreed, with the
    commands printing a red error panel while the palette offered fixes.

    Split by cause: a catalog with models in it but none reachable is a "start
    your server" problem, while an empty catalog is a "you have not connected
    anything yet" problem. Telling someone to add an API key when they already
    have one and Ollama is simply stopped sends them down the wrong path.

    Neither palette pane wraps, so an over-long line is silently clipped.
    Measured against an 80-column terminal (the narrow case worth supporting):
    the list pane gets ~32 columns and the details pane ~41, so headlines stay
    under 28 and detail/remedy lines under 38.
    """
    if has_registered_models:
        return (
            "No models reachable",
            "Known models are not responding.",
            [
                ("Start your local server", "ollama serve"),
                ("Check a provider key", "/providers"),
                ("Re-scan once it is up", "/model discover"),
            ],
        )
    return (
        "No models connected yet",
        "Connect a provider or a local model.",
        [
            ("Add a cloud provider key", "/connect"),
            ("Or pull a local model", "ollama pull qwen2.5-coder:7b"),
            ("Then discover them", "/model discover"),
        ],
    )


class ModelPaletteModel:
    """Filtering and keyboard selection state, independent of any terminal."""

    def __init__(self, models_source: Callable[[], list[Any]]) -> None:
        self._models_source = models_source
        self.selected_index = 0
        self._last_query: str | None = None

    @staticmethod
    def parse(text: str) -> tuple[str, str] | None:
        """Split *text* into ``(trigger, filter query)``, or None if it is not a
        model-palette context.

        The trigger is returned so the header can echo what was actually typed
        (``/models`` vs ``/model``) instead of silently rewriting it.
        """
        stripped = text.lstrip()
        for trigger in _TRIGGERS:
            if not stripped.startswith(trigger):
                continue
            rest = stripped[len(trigger) :]
            # "/modelx" is a different command, not "/model" + a filter. (The
            # "/models" case is already handled by trying it before "/model".)
            if rest and not rest[0].isspace():
                continue
            query = rest.strip()
            # A subcommand means a command is being typed, not a model picked.
            if query and query.split()[0].lower() in _SUBCOMMANDS:
                return None
            return trigger, query
        return None

    @classmethod
    def query_from_text(cls, text: str) -> str | None:
        """The filter query for *text*, or None if the palette should stay shut."""
        parsed = cls.parse(text)
        return None if parsed is None else parsed[1]

    def matches(self, query: str) -> list[ModelMatch]:
        if query != self._last_query:
            self.selected_index = 0
            self._last_query = query

        needle = query.strip().lower()
        matched = [
            model
            for model in self._models_source()
            if not needle
            or needle in str(model.model_id).lower()
            or needle in str(getattr(model, "display_name", "") or "").lower()
            or needle in str(model.provider_id).lower()
        ]
        results = [
            ModelMatch(model=model, group="Local" if model.is_local else "Cloud")
            for model in matched
        ]

        if results:
            self.selected_index = min(self.selected_index, len(results) - 1)
        else:
            self.selected_index = 0
        return results

    def move(self, query: str, amount: int) -> None:
        matches = self.matches(query)
        if matches:
            self.selected_index = (self.selected_index + amount) % len(matches)

    def selected(self, query: str) -> Any | None:
        matches = self.matches(query)
        if not matches:
            return None
        return matches[self.selected_index].model


class ModelPalette:
    """prompt_toolkit renderer and key bindings for :class:`ModelPaletteModel`."""

    def __init__(
        self,
        container: Any,
        *,
        suppressed: Callable[[], bool] | None = None,
        models_source: Callable[[], list[Any]] | None = None,
        registered_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._container = container
        # "Something else owns the prompt box" — e.g. an InlineFlow step whose
        # filter text could itself look like a /model invocation.
        self._suppressed = suppressed
        self._models_source = models_source or (lambda: connected_models(container))
        self._registered_probe = registered_probe or (lambda: has_any_registered_model(container))
        self.model = ModelPaletteModel(self._models_source)
        self._dismissed_text: str | None = None

    # -- state ------------------------------------------------------------

    def _buffer_text(self) -> str:
        try:
            return get_app().current_buffer.text
        except Exception:
            return ""

    def query(self) -> str | None:
        return self.model.query_from_text(self._buffer_text())

    def _trigger(self) -> str:
        """The command head the user actually typed, for echoing in the header."""
        parsed = self.model.parse(self._buffer_text())
        return parsed[0] if parsed else _TRIGGERS[-1]

    def is_active(self) -> bool:
        if self._suppressed is not None and self._suppressed():
            return False
        text = self._buffer_text()
        return self.model.query_from_text(text) is not None and text != self._dismissed_text

    def dismiss(self) -> None:
        self._dismissed_text = self._buffer_text()

    def _selected(self) -> Any | None:
        query = self.query()
        return self.model.selected(query) if query is not None else None

    @staticmethod
    def _window_start(total: int, selected: int, limit: int) -> int:
        if total <= limit:
            return 0
        return min(max(0, selected - limit // 2), total - limit)

    # -- empty state ------------------------------------------------------

    def _empty_reason(self) -> tuple[str, str, list[tuple[str, str]]]:
        """This palette's view of :func:`empty_state_guidance`."""
        return empty_state_guidance(self._registered_probe())

    # -- rendering --------------------------------------------------------

    def render_models(self) -> FormattedText:
        query = self.query() or ""
        matches = self.model.matches(query)
        count = len(matches)
        lines: list[tuple[str, str]] = [
            ("class:model-palette.label", "  MODEL\n"),
            ("class:model-palette.query", f"  {self._trigger()} {query}".rstrip()),
            ("class:model-palette.muted", f"  {count} model{'s' if count != 1 else ''}\n\n"),
        ]

        if not matches:
            if query:
                # There *are* models, they just don't match what was typed —
                # that is a filter miss, not a setup problem.
                lines.append(("class:model-palette.warning", f"  No model matches “{query}”\n"))
                return FormattedText(lines)
            # Only the headline here — this pane is ~34 columns, too narrow for
            # a command like "ollama pull qwen2.5-coder:7b" without truncating.
            # The remedies render in the wide details pane instead.
            headline, _detail, _remedies = self._empty_reason()
            lines.append(("class:model-palette.warning", f"  {headline}\n\n"))
            lines.append(("class:model-palette.muted", "  See the panel to the right\n"))
            lines.append(("class:model-palette.muted", "  for how to fix this.\n"))
            return FormattedText(lines)

        start = self._window_start(count, self.model.selected_index, _VISIBLE_ROWS)
        visible = matches[start : start + _VISIBLE_ROWS]
        previous_group: str | None = None
        for offset, match in enumerate(visible):
            index = start + offset
            model = match.model
            if match.group != previous_group:
                if previous_group is not None:
                    lines.append(("", "\n"))
                lines.append(("class:model-palette.group", f"  {match.group.upper()}\n"))
                previous_group = match.group
            selected = index == self.model.selected_index
            marker = ">" if selected else " "
            style = "class:model-palette.selected" if selected else "class:model-palette.model"
            name = str(getattr(model, "display_name", "") or model.model_id)
            if len(name) > 30:
                name = name[:29].rstrip() + "…"
            lines.append((style, f" {marker} {name:<31}"))
            lines.append(("class:model-palette.muted", f"{model.provider_id}\n"))

        end = start + len(visible)
        if start > 0 or end < count:
            lines.append(("class:model-palette.muted", f"\n  {start + 1}-{end} of {count}\n"))
        return FormattedText(lines)

    def render_details(self) -> FormattedText:
        model = self._selected()
        if model is None:
            if self.model.matches(self.query() or ""):
                return FormattedText(
                    [("class:model-palette.muted", "  Use ↑/↓ to choose a model.")]
                )
            # Nothing to describe, so this pane becomes the setup guide — it is
            # the wider of the two, so full commands fit here without wrapping.
            headline, detail, remedies = self._empty_reason()
            lines: list[tuple[str, str]] = [
                ("class:model-palette.title", f"  {headline}\n"),
                ("class:model-palette.muted", f"  {detail}\n\n"),
                ("class:model-palette.label", "  HOW TO FIX\n"),
            ]
            for label, command in remedies:
                lines.append(("class:model-palette.text", f"  {label}\n"))
                lines.append(("class:model-palette.code", f"    {command}\n"))
            lines.append(("class:model-palette.muted", "\n  Esc close"))
            return FormattedText(lines)

        tag = "local" if model.is_local else "cloud"
        context_length = getattr(model, "context_length", 0) or 0
        lines: list[tuple[str, str]] = [
            ("class:model-palette.title", f"  {model.model_id}\n"),
            ("class:model-palette.muted", f"  {model.provider_id} · {tag}\n\n"),
            ("class:model-palette.label", "  CONTEXT\n"),
            ("class:model-palette.code", f"  {context_length:,} tokens\n\n"),
        ]

        capabilities = getattr(model, "capabilities", None)
        if capabilities is not None:
            enabled = [
                label
                for label, attr in (
                    ("vision", "vision"),
                    ("tools", "tool_use"),
                    ("reasoning", "reasoning"),
                    ("embedding", "embedding"),
                )
                if getattr(capabilities, attr, False)
            ]
            lines.append(("class:model-palette.label", "  CAPABILITIES\n"))
            lines.append(
                ("class:model-palette.code", f"  {', '.join(enabled) if enabled else '—'}\n\n")
            )

        speed = getattr(model, "speed_tier", None)
        health = getattr(model, "health", None)
        if speed or health:
            lines.append(("class:model-palette.label", "  STATUS\n"))
            lines.append(
                ("class:model-palette.code", f"  {health or 'unknown'} · {speed or 'medium'}\n\n")
            )

        location = getattr(model, "location", None)
        if location:
            shown = str(location)
            if len(shown) > 34:
                shown = "…" + shown[-33:]
            lines.append(("class:model-palette.label", "  LOCATION\n"))
            lines.append(("class:model-palette.code", f"  {shown}\n\n"))

        lines.append(("class:model-palette.muted", "  ↑/↓ navigate   Enter select   Esc close"))
        return FormattedText(lines)

    # -- wiring -----------------------------------------------------------

    def add_bindings(self, bindings: KeyBindings) -> None:
        active = Condition(self.is_active)

        @bindings.add("up", filter=active, eager=True)
        def _up(event) -> None:
            self.model.move(self.query() or "", -1)
            event.app.invalidate()

        @bindings.add("down", filter=active, eager=True)
        def _down(event) -> None:
            self.model.move(self.query() or "", 1)
            event.app.invalidate()

        @bindings.add("enter", filter=active, eager=True)
        def _select(event) -> None:
            model = self._selected()
            if model is None:
                # Nothing to pick (empty catalog): swallow Enter rather than
                # submitting a bare "/model", which would dump a second,
                # redundant listing under a palette already explaining this.
                return
            # Route through the ordinary command path so persistence, recents,
            # and the default-provider write all happen exactly as they do for
            # a hand-typed switch.
            event.current_buffer.text = f"/model use {model.model_id}"
            event.current_buffer.cursor_position = len(event.current_buffer.text)
            self._dismissed_text = None
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", filter=active, eager=True)
        def _close(event) -> None:
            self.dismiss()
            event.app.invalidate()

    def container(self) -> ConditionalContainer:
        left = Window(
            content=FormattedTextControl(self.render_models),
            width=Dimension(min=25, preferred=34),
            dont_extend_width=False,
        )
        divider = Window(width=1, char="|", style="class:model-palette.border")
        right = Window(
            content=FormattedTextControl(self.render_details),
            width=Dimension(min=30, weight=2),
        )
        body = VSplit([left, divider, right], padding=1, padding_style="class:model-palette.border")
        frame = Frame(
            body,
            title=[("class:model-palette.frame-title", " MODEL PALETTE ")],
            style="class:model-palette.frame",
        )
        return ConditionalContainer(frame, filter=Condition(self.is_active))

    def attach(self, session: PromptSession) -> None:
        """Overlay the palette on a PromptSession without replacing its buffer."""
        root = session.layout.container
        session.layout.container = FloatContainer(
            content=root,
            floats=[
                Float(
                    content=self.container(),
                    left=2,
                    right=2,
                    top=1,
                    height=18,
                    allow_cover_cursor=True,
                    z_index=20,
                )
            ],
        )


#: Mirrors PALETTE_STYLES role-for-role, swapping indigo for cyan-blue so the
#: two palettes are visually siblings rather than the same surface.
MODEL_PALETTE_STYLES: dict[str, str] = {
    "model-palette.frame": f"bg:{design.SURFACE} fg:{design.FAINT}",
    "model-palette.frame-title": f"bg:{design.SURFACE} fg:{design.CYAN} bold",
    "model-palette.border": f"bg:{design.SURFACE} fg:{design.FAINT}",
    "model-palette.title": f"bg:{design.SURFACE} fg:{design.WHITE} bold",
    "model-palette.label": f"bg:{design.SURFACE} fg:{design.MUTED} bold",
    "model-palette.query": f"bg:{design.SURFACE} fg:{design.CYAN} bold",
    "model-palette.group": f"bg:{design.SURFACE} fg:{design.MUTED} bold",
    "model-palette.model": f"bg:{design.SURFACE} fg:{design.WHITE}",
    "model-palette.selected": f"bg:{design.LIGHT_BG} fg:{design.CYAN} bold",
    "model-palette.text": f"bg:{design.SURFACE} fg:{design.WHITE}",
    "model-palette.code": f"bg:{design.SURFACE} fg:{design.CYAN_SOFT}",
    "model-palette.muted": f"bg:{design.SURFACE} fg:{design.FAINT}",
    "model-palette.warning": f"bg:{design.SURFACE} fg:{design.WARN}",
}
