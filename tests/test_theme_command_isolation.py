"""``/theme`` is consumed by the command system and never reaches inference.

A slash command that falls through to the prompt is not merely untidy: it
sends the literal text "/theme" to a model, bills a request for it, and leaves
the command in the transcript. The theme palette binds Enter itself, so this
suite pins the two properties that make that binding correct — the keypress is
consumed, and the prompt editor is left clean.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from velune.cli import design, themes
from velune.cli.theme_palette import ThemePalette


class _Buffer:
    """The slice of prompt_toolkit's Buffer the Enter binding touches."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.cursor_position = len(text)
        self.validate_and_handle_calls = 0

    def validate_and_handle(self) -> None:
        # This is the call that submits the line — i.e. sends it to inference.
        self.validate_and_handle_calls += 1


class _App:
    def __init__(self) -> None:
        self.invalidated = 0

    def invalidate(self) -> None:
        self.invalidated += 1


class _Event:
    def __init__(self, text: str) -> None:
        self.current_buffer = _Buffer(text)
        self.app = _App()


class _Bindings:
    """Captures handlers by key name instead of standing up a real KeyBindings."""

    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}

    def add(self, key, **_kwargs):
        def decorator(fn):
            self.handlers[key] = fn
            return fn

        return decorator


@pytest.fixture(autouse=True)
def _restore_default_theme():
    before = design.active_theme()
    yield
    design.apply_theme(before)


def _palette_with_bindings(buffer_text: str, on_select=None):
    palette = ThemePalette(on_select=on_select, themes_source=themes.list_themes)
    # Drive the model off supplied text rather than a live prompt_toolkit app.
    palette._buffer_text = lambda: buffer_text  # type: ignore[method-assign]
    bindings = _Bindings()
    palette.add_bindings(bindings)
    return palette, bindings


# --- the palette opens ---------------------------------------------------------


def test_bare_theme_activates_the_palette():
    palette, _ = _palette_with_bindings("/theme")
    assert palette.is_active() is True


def test_escape_dismisses_without_submitting():
    palette, bindings = _palette_with_bindings("/theme")
    event = _Event("/theme")
    bindings.handlers["escape"](event)

    assert palette.is_active() is False
    assert event.current_buffer.validate_and_handle_calls == 0


# --- Enter never submits -------------------------------------------------------


def test_enter_never_submits_the_line_to_inference():
    _palette, bindings = _palette_with_bindings("/theme")
    event = _Event("/theme")

    bindings.handlers["enter"](event)

    assert event.current_buffer.validate_and_handle_calls == 0, (
        "/theme was submitted as a prompt and would have reached the model"
    )


def test_enter_clears_the_prompt_editor():
    _palette, bindings = _palette_with_bindings("/theme")
    event = _Event("/theme")

    bindings.handlers["enter"](event)

    assert event.current_buffer.text == ""
    assert event.current_buffer.cursor_position == 0


def test_enter_with_a_filter_also_leaves_no_text_behind():
    _palette, bindings = _palette_with_bindings("/theme emer")
    event = _Event("/theme emer")

    bindings.handlers["enter"](event)

    assert event.current_buffer.text == ""
    assert event.current_buffer.validate_and_handle_calls == 0


def test_enter_on_a_query_matching_nothing_still_consumes_the_key():
    """No match means nothing to apply — but submitting "/theme zzz" to the
    model would be worse than doing nothing."""
    _palette, bindings = _palette_with_bindings("/theme zzzzz")
    event = _Event("/theme zzzzz")

    bindings.handlers["enter"](event)

    assert event.current_buffer.validate_and_handle_calls == 0
    assert event.current_buffer.text == ""


def test_the_enter_binding_never_calls_validate_and_handle_at_all():
    """Belt and braces against a future edit reintroducing a submit path: no
    executable line in the module may call the submit method.

    Comments are stripped first — the binding's own comment explains *why* it
    does not call ``validate_and_handle``, and matching that would make this
    test pass or fail on prose.
    """
    src = Path("velune/cli/theme_palette.py").read_text(encoding="utf-8")
    code_only = "\n".join(
        re.sub(r"#.*$", "", line) for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert "validate_and_handle" not in code_only


# --- selection applies ---------------------------------------------------------


def test_enter_applies_the_selected_theme():
    applied: list[str] = []
    _palette, bindings = _palette_with_bindings(
        "/theme emerald", on_select=lambda theme: applied.append(theme.id)
    )

    bindings.handlers["enter"](_Event("/theme emerald"))

    assert applied == ["emerald"]


def test_arrow_then_enter_applies_the_row_under_the_cursor():
    applied: list[str] = []
    palette, bindings = _palette_with_bindings(
        "/theme", on_select=lambda theme: applied.append(theme.id)
    )

    bindings.handlers["down"](_Event("/theme"))  # velune -> amber
    bindings.handlers["down"](_Event("/theme"))  # amber  -> emerald
    bindings.handlers["enter"](_Event("/theme"))

    assert applied == ["emerald"]
    assert palette is not None


def test_selection_is_reported_through_the_callback_not_applied_directly():
    """The palette must not write config or mutate design itself — the REPL
    owns "apply + persist" so the palette, the /theme command form, and the
    settings TUI all commit a choice the same way."""
    before = design.active_theme_id()
    _palette, bindings = _palette_with_bindings("/theme crimson", on_select=None)

    bindings.handlers["enter"](_Event("/theme crimson"))

    assert design.active_theme_id() == before
