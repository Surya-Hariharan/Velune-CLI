"""Tests for PaletteSelectWidget — the command-palette-styled picker used by
/connect and /providers.

The interaction tests drive the widget through ``run_standalone`` with real key
events (prompt_toolkit's pipe-input harness), not by calling handlers directly.
That is the point of them: the widget supplies its own container instead of the
single focusable ``Window`` the runner builds by default, so "do arrow keys and
Enter still reach it?" is a real question, not a formality.
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from velune.cli.interactive.result import BACK
from velune.cli.interactive.runner import run_standalone
from velune.cli.interactive.widgets import Option, PaletteSelectWidget, TextInputWidget

OPTIONS = [
    Option("openai", "OpenAI", "GPT models", group="Cloud", badge="verified"),
    Option("anthropic", "Anthropic", "Claude models", group="Cloud"),
    Option("ollama", "Ollama", "Local models", group="Local", badge="running"),
]


def _widget(**kwargs) -> PaletteSelectWidget:
    params = {"title": "Connect Provider", "options": list(OPTIONS)}
    params.update(kwargs)
    return PaletteSelectWidget(**params)


def _text(fragments) -> str:
    return "".join(t for _style, t in fragments)


# ── Rendering ────────────────────────────────────────────────────────────────


def test_results_pane_shows_search_header_and_groups_when_filterable():
    text = _text(_widget(filterable=True).render_results())

    assert "SEARCH" in text
    assert "3 results" in text
    assert "CLOUD" in text and "LOCAL" in text
    assert "OpenAI" in text


def test_results_pane_omits_search_header_for_fixed_menus():
    text = _text(_widget(filterable=False).render_results())

    assert "OPTIONS" in text
    assert "SEARCH" not in text


def test_details_pane_describes_the_highlighted_row():
    widget = _widget(filterable=True)
    widget.move(1)  # highlight Anthropic

    text = _text(widget.render_details())
    assert "Anthropic" in text
    assert "Claude models" in text
    assert "DESCRIPTION" in text


def test_details_pane_shows_badge_as_status():
    text = _text(_widget(filterable=True).render_details())

    assert "STATUS" in text
    assert "verified" in text


def test_filtering_narrows_results_and_retargets_details():
    widget = _widget(filterable=True)
    widget._filter = "olla"

    assert "1 result" in _text(widget.render_results())
    assert "Ollama" in _text(widget.render_details())


def test_no_matches_renders_a_warning_not_a_crash():
    widget = _widget(filterable=True)
    widget._filter = "zzzzz"

    assert "No matches" in _text(widget.render_results())
    assert "Nothing matches" in _text(widget.render_details())


def test_long_option_lists_scroll_instead_of_growing_the_panel():
    many = [Option(f"p{i}", f"Provider {i}", "desc") for i in range(40)]
    widget = _widget(options=many, filterable=True)

    rendered_rows = _text(widget.render_results()).count("Provider ")
    assert rendered_rows <= 9


# ── Real key-driven interaction ──────────────────────────────────────────────


def _drive(widget, keys: str):
    async def _run():
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return await run_standalone(widget, input=pipe, output=DummyOutput())

    return asyncio.run(_run())


def test_arrow_keys_and_enter_reach_a_widget_with_its_own_container():
    # Down once, then Enter -> the second option.
    assert _drive(_widget(filterable=True), "\x1b[B\r") == "anthropic"


def test_enter_selects_the_first_row_by_default():
    assert _drive(_widget(filterable=True), "\r") == "openai"


def test_typing_filters_then_enter_selects_the_match():
    assert _drive(_widget(filterable=True), "olla\r") == "ollama"


def test_escape_backs_out():
    assert _drive(_widget(filterable=True), "\x1b") is BACK


def test_panelled_text_input_still_submits_its_value():
    async def _run():
        with create_pipe_input() as pipe:
            pipe.send_text("sk-test-key\r")
            return await run_standalone(
                TextInputWidget(title="OpenAI API key", password=True, panelled=True),
                input=pipe,
                output=DummyOutput(),
            )

    assert asyncio.run(_run()) == "sk-test-key"


def test_panelled_widgets_suppress_the_hosts_duplicate_hint_strip():
    assert PaletteSelectWidget(title="t", options=list(OPTIONS)).shows_own_hints is True
    assert TextInputWidget(title="t", panelled=True).shows_own_hints is True
    assert TextInputWidget(title="t").shows_own_hints is False
