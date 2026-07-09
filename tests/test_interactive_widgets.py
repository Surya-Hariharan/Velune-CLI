"""Unit tests for the reusable single-select/multi-select/text-input widgets
that back onboarding, setup, and REPL pickers alike (velune/cli/interactive).

These exercise the widgets' internal navigation/toggle/submit logic directly
(no Application/event loop needed) — see test_wizard_chrome.py for a real
keystroke-driven end-to-end test of the same SelectWidget.
"""

from __future__ import annotations

from types import SimpleNamespace

from velune.cli.interactive.widgets import ConfirmWidget, Option, SelectWidget, TextInputWidget
from velune.providers import catalog

# ── SelectWidget ─────────────────────────────────────────────────────────────


def _options() -> list[Option]:
    return [Option("a", "Alpha"), Option("b", "Beta"), Option("c", "Gamma")]


def test_select_widget_move_wraps_around():
    w = SelectWidget(title="t", options=_options())
    assert w._index == 0
    w._move(-1)
    assert w._index == 2  # wraps to last
    w._move(1)
    assert w._index == 0


def test_select_widget_single_submit_returns_highlighted_id():
    w = SelectWidget(title="t", options=_options())
    captured = {}
    w.on_submit = lambda v: captured.setdefault("v", v)
    w._move(1)  # -> Beta
    w._submit()
    assert captured["v"] == "b"


def test_select_widget_multi_toggle_and_submit_preserves_option_order():
    w = SelectWidget(title="t", options=_options(), multiple=True)
    w._move(1)
    w._toggle_current()  # check b
    w._move(1)
    w._toggle_current()  # check c
    captured = {}
    w.on_submit = lambda v: captured.setdefault("v", v)
    w._submit()
    assert captured["v"] == ["b", "c"]  # original options order, not toggle order


def test_select_widget_toggle_is_idempotent_reversible():
    w = SelectWidget(title="t", options=_options(), multiple=True)
    w._toggle_current()
    assert "a" in w._checked
    w._toggle_current()
    assert "a" not in w._checked


def test_select_widget_initial_checked_prefills_state():
    w = SelectWidget(title="t", options=_options(), multiple=True, initial_checked=frozenset({"b"}))
    assert w._checked == {"b"}


def test_select_widget_filterable_narrows_visible_options():
    w = SelectWidget(title="t", options=_options(), filterable=True)
    w._filter = "gamma"
    visible = w._visible()
    assert [o.id for o in visible] == ["c"]


def test_select_widget_footer_hint_reflects_mode():
    single = SelectWidget(title="t", options=_options())
    multi = SelectWidget(title="t", options=_options(), multiple=True)
    assert "Space toggle" not in single.footer_hint()
    assert "Space toggle" in multi.footer_hint()
    assert "Esc back" in single.footer_hint()


# ── TextInputWidget ──────────────────────────────────────────────────────────


def test_text_input_submits_stripped_value():
    w = TextInputWidget(title="Key")
    captured = {}
    w.on_submit = lambda v: captured.setdefault("v", v)
    w._on_accept(SimpleNamespace(text="  secret-key  "))
    assert captured["v"] == "secret-key"


def test_text_input_optional_allows_empty_submit():
    w = TextInputWidget(title="Key", optional=True)
    captured = {}
    w.on_submit = lambda v: captured.setdefault("v", v)
    w._on_accept(SimpleNamespace(text=""))
    assert captured["v"] == ""


def test_text_input_validate_error_blocks_submit():
    w = TextInputWidget(title="Key", validate=lambda v: "too short" if len(v) < 5 else None)
    captured = {}
    w.on_submit = lambda v: captured.setdefault("v", v)
    w._on_accept(SimpleNamespace(text="abc"))
    assert "v" not in captured
    assert w._error == "too short"


def test_text_input_validate_pass_submits():
    w = TextInputWidget(title="Key", validate=lambda v: "too short" if len(v) < 5 else None)
    captured = {}
    w.on_submit = lambda v: captured.setdefault("v", v)
    w._on_accept(SimpleNamespace(text="abcdef"))
    assert captured["v"] == "abcdef"


# ── ConfirmWidget ────────────────────────────────────────────────────────────


def test_confirm_widget_defaults_to_configured_choice():
    w = ConfirmWidget(question="Continue?", default=False)
    assert w._choice is False
    lines = w.render()
    assert any("Continue?" in text for _style, text in lines)


# ── Provider catalog ordering (spec item 3: alphabetical, never by price) ────


def test_catalog_all_providers_alphabetical_and_mixed():
    names = [p.display_name for p in catalog.list_providers_alphabetical()]
    assert names == sorted(names, key=str.lower)
    # Local (no-key) providers are interleaved by name, not grouped separately.
    assert "Ollama" in names
    assert "LM Studio" in names


def test_catalog_cloud_only_excludes_local_providers():
    cloud = catalog.list_cloud_providers_alphabetical()
    assert all(p.requires_key for p in cloud)
    ids = {p.id for p in cloud}
    assert "ollama" not in ids
    assert "lmstudio" not in ids


def test_catalog_recommended_free_start_never_reorders_list():
    # RECOMMENDED_FREE_START is advisory only per catalog.py's own docstring.
    names = [p.display_name for p in catalog.list_providers_alphabetical()]
    assert names == sorted(names, key=str.lower)
    assert isinstance(catalog.RECOMMENDED_FREE_START, tuple)
