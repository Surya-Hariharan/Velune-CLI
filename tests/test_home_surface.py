"""Tests for velune.cli.home — the compact empty-transcript home surface.

The redesign replaced the full-screen centered VELUNE wordmark with an
upper-left header (brand, version, model · provider, workspace · branch)
plus a runtime summary block. These tests cover the pure renderer and its
wiring into FullscreenREPLUI.
"""

from __future__ import annotations

from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import DummyOutput

from velune.cli.fullscreen import FullscreenREPLUI
from velune.cli.home import HomeState, render_home


def _text(fragments) -> str:
    return "".join(t for _s, t in fragments)


def _full_state() -> HomeState:
    return HomeState(
        version="0.9.6",
        model_id="llama-3.3-70b-versatile",
        provider="groq",
        workspace_path=r"C:\Projects\MyApp",
        git_branch="main",
        project_type="Python",
        indexed_files=412,
        memory_label="cognitive 76.4 MB",
        mcp_connected=2,
        mcp_total=3,
        providers=("groq", "openai"),
        local_runtime="Ollama",
    )


# ── Header ───────────────────────────────────────────────────────────────────


def test_header_shows_brand_version_model_provider_and_workspace():
    text = _text(render_home(_full_state(), width=100))
    assert "VELUNE CLI" in text
    assert "v0.9.6" in text
    assert "llama-3.3-70b-versatile" in text
    assert "Groq" in text
    assert "MyApp" in text
    assert "main" in text


def test_header_is_upper_left_not_centered():
    # First content line starts after the small margin — no centering padding.
    lines = _text(render_home(_full_state(), width=120)).split("\n")
    brand_line = next(line for line in lines if "VELUNE CLI" in line)
    assert brand_line.startswith("  VELUNE")


def test_no_model_shows_setup_pointer_instead_of_model_line():
    state = _full_state()
    state.model_id = None
    text = _text(render_home(state, width=100))
    assert "no model selected" in text
    assert "/model" in text


# ── Runtime summary rows ─────────────────────────────────────────────────────


def test_summary_rows_cover_repo_memory_mcp_providers_and_local():
    text = _text(render_home(_full_state(), width=100))
    assert "Repository" in text and "Python" in text and "412 files indexed" in text
    assert "Memory" in text and "cognitive 76.4 MB" in text
    assert "MCP" in text and "2/3 servers connected" in text
    assert "Providers" in text and "Groq +1 more" in text
    assert "Local" in text and "Ollama" in text


def test_empty_state_degrades_to_quiet_defaults():
    text = _text(render_home(HomeState(), width=100))
    assert "none detected" in text  # repository
    assert "no servers configured" in text  # mcp
    assert "/setup" in text  # providers
    assert "Local" not in text  # row omitted when nothing local


def test_unindexed_repository_says_so():
    state = _full_state()
    state.indexed_files = None
    text = _text(render_home(state, width=100))
    assert "not indexed yet" in text


def test_narrow_terminal_clips_values_instead_of_overflowing():
    state = _full_state()
    state.model_id = "a-very-long-model-identifier-that-cannot-possibly-fit"
    fragments = render_home(state, width=40)
    for line in _text(fragments).split("\n"):
        assert len(line) <= 40, f"line overflows 40 cols: {line!r}"


def test_hint_line_teaches_palette_and_file_mentions():
    text = _text(render_home(_full_state(), width=100))
    assert "/ commands" in text
    assert "@@file" in text


# ── Fullscreen wiring ────────────────────────────────────────────────────────


def _make_ui(home_provider=None) -> FullscreenREPLUI:
    return FullscreenREPLUI(
        status_state=None,
        history=InMemoryHistory(),
        completer=None,
        validator=None,
        style_fragments={},
        key_bindings=KeyBindings(),
        on_interrupt=lambda e: True,
        home_provider=home_provider,
        output=DummyOutput(),
    )


def test_empty_transcript_renders_home_state_from_provider():
    ui = _make_ui(home_provider=_full_state)
    text = _text(ui._render_conversation())
    assert "VELUNE CLI" in text
    assert "llama-3.3-70b-versatile" in text


def test_transcript_content_replaces_home_surface():
    ui = _make_ui(home_provider=_full_state)
    ui.append_user("hello")
    text = _text(ui._render_conversation())
    assert "hello" in text
    assert "Repository" not in text


def test_home_provider_failure_falls_back_to_default_state():
    def _boom():
        raise RuntimeError("provider exploded")

    ui = _make_ui(home_provider=_boom)
    text = _text(ui._render_conversation())
    assert "VELUNE CLI" in text  # renders, does not crash


def test_no_provider_renders_minimal_default():
    ui = _make_ui(home_provider=None)
    text = _text(ui._render_conversation())
    assert "VELUNE CLI" in text


def test_first_submit_goes_straight_to_queue_no_animation_state():
    # The old logo slide-up animation deferred the first submit; the redesign
    # queues immediately.
    import asyncio

    async def _run():
        ui = _make_ui(home_provider=_full_state)
        ui.submit("first message")
        return await asyncio.wait_for(ui.read_input(), timeout=1.0)

    assert asyncio.run(_run()) == "first message"
