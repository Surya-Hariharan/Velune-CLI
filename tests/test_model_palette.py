"""The /model palette: trigger conditions, arrow selection, and empty states.

The palette shares the command palette's float rectangle, so the two must never
be active at once — most of these tests pin that handover rather than the
rendering, since a double-render is the failure the user would actually see.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from velune.cli.model_palette import (
    MODEL_PALETTE_STYLES,
    ModelPalette,
    ModelPaletteModel,
    connected_models,
    has_any_registered_model,
)


def _model(model_id, provider_id="ollama", *, is_local=True, display_name=None):
    return SimpleNamespace(
        model_id=model_id,
        provider_id=provider_id,
        display_name=display_name or model_id,
        is_local=is_local,
        context_length=8192,
        capabilities=SimpleNamespace(vision=False, tool_use=True, reasoning=True, embedding=False),
        speed_tier="medium",
        health="healthy",
        location=None,
    )


def _palette(models, *, registered=None, suppressed=None):
    return ModelPalette(
        container=None,
        suppressed=suppressed,
        models_source=lambda: list(models),
        registered_probe=(lambda: registered if registered is not None else bool(models)),
    )


# --- trigger conditions -------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/model", ""),  # the exact ask: palette opens as soon as /model is typed
        ("/model ", ""),
        ("  /model", ""),  # leading whitespace is not meaningful
        ("/model llama", "llama"),  # trailing text filters the list
        ("/model  qwen  ", "qwen"),
        # /models is the listing command; it opens the same picker rather than
        # submitting and printing a "No models available" error panel.
        ("/models", ""),
        ("/models ", ""),
        ("/models llama", "llama"),
    ],
)
def test_opens_on_the_model_command(text, expected):
    assert ModelPaletteModel.query_from_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "/",
        "/mod",  # still typing toward /model
        "/mode",
        "/modelx",
        "/modelsx",
        "hello /model",  # not a command head at all
        "explain the /model flag",
    ],
)
def test_stays_shut_for_anything_that_is_not_the_model_command(text):
    assert ModelPaletteModel.query_from_text(text) is None


@pytest.mark.parametrize(
    "text,trigger",
    [
        ("/model", "/model"),
        ("/models", "/models"),
        ("/models qwen", "/models"),
    ],
)
def test_parse_reports_the_trigger_that_was_typed(text, trigger):
    """ "/models" must not be mis-parsed as "/model" + a stray "s", and the
    header echoes what the user typed rather than rewriting it."""
    parsed = ModelPaletteModel.parse(text)
    assert parsed is not None
    assert parsed[0] == trigger


@pytest.mark.parametrize(
    "sub", ["discover", "connect", "use", "list", "status", "remove", "locate", "locations"]
)
def test_subcommands_never_open_the_picker(sub):
    """`/model use gpt-4o` is a command being typed, not a model being picked.

    If the palette opened here, Enter would submit the palette's selection
    instead of the command the user was halfway through writing.
    """
    assert ModelPaletteModel.query_from_text(f"/model {sub}") is None
    assert ModelPaletteModel.query_from_text(f"/model {sub} some-arg") is None


def test_subcommand_matching_is_case_insensitive():
    assert ModelPaletteModel.query_from_text("/model USE gpt-4o") is None


# --- arrow navigation ---------------------------------------------------------


def test_up_and_down_cycle_the_selection():
    models = [_model("a"), _model("b"), _model("c")]
    m = ModelPaletteModel(lambda: models)

    assert m.selected("").model_id == "a"
    m.move("", 1)
    assert m.selected("").model_id == "b"
    m.move("", 1)
    assert m.selected("").model_id == "c"
    # The arrow keys wrap rather than dead-ending at the list edges.
    m.move("", 1)
    assert m.selected("").model_id == "a"
    m.move("", -1)
    assert m.selected("").model_id == "c"


def test_selection_resets_when_the_filter_changes():
    models = [_model("alpha"), _model("beta")]
    m = ModelPaletteModel(lambda: models)
    m.move("", 1)
    assert m.selected("").model_id == "beta"

    # Typing narrows the list; keeping index 1 could point past the end.
    assert m.selected("alpha").model_id == "alpha"
    assert m.selected_index == 0


def test_navigation_on_an_empty_list_is_a_no_op():
    m = ModelPaletteModel(list)
    m.move("", 1)
    m.move("", -1)
    assert m.selected("") is None
    assert m.selected_index == 0


def test_filter_matches_id_display_name_and_provider():
    models = [
        _model("llama3.2", "ollama"),
        _model("gpt-4o", "openai", is_local=False, display_name="GPT-4o Omni"),
    ]
    m = ModelPaletteModel(lambda: models)

    assert [x.model.model_id for x in m.matches("llama")] == ["llama3.2"]
    assert [x.model.model_id for x in m.matches("omni")] == ["gpt-4o"]
    assert [x.model.model_id for x in m.matches("openai")] == ["gpt-4o"]
    assert m.matches("nonexistent") == []


def test_models_are_grouped_local_then_cloud():
    models = [_model("local-one"), _model("cloud-one", "openai", is_local=False)]
    m = ModelPaletteModel(lambda: models)
    assert [x.group for x in m.matches("")] == ["Local", "Cloud"]


# --- empty states -------------------------------------------------------------


def _text(formatted):
    return "".join(fragment for _style, fragment in formatted)


def _both_panes(palette) -> str:
    """Everything the user sees: the list pane plus the details pane.

    The empty-state guidance deliberately lives in the wider details pane (the
    list pane is too narrow for a full `ollama pull ...` line), so assertions
    about what the user is told must span both.
    """
    return _text(palette.render_models()) + "\n" + _text(palette.render_details())


def test_empty_catalog_tells_you_to_connect_a_provider_or_pull_a_model():
    """The user's requirement: the palette still shows, with guidance."""
    palette = _palette([], registered=False)
    out = _both_panes(palette)

    assert "No models connected yet" in out
    assert "/connect" in out
    assert "ollama pull" in out


def test_models_present_but_unreachable_says_start_the_server_instead():
    """Telling someone to add an API key when they have one and Ollama is
    simply stopped sends them down the wrong path."""
    palette = _palette([], registered=True)
    out = _both_panes(palette)

    assert "No models reachable" in out
    assert "ollama serve" in out
    assert "ollama pull" not in out


class _FakeBufferPalette(ModelPalette):
    """ModelPalette with a scriptable prompt buffer.

    Outside a running prompt_toolkit Application there is no buffer to read, so
    overriding this one seam is what lets the query-dependent render paths be
    exercised headlessly.
    """

    def __init__(self, *args, buffer_text="", **kwargs):
        super().__init__(*args, **kwargs)
        self.buffer_text = buffer_text

    def _buffer_text(self) -> str:
        return self.buffer_text


def test_a_filter_miss_is_not_reported_as_a_setup_problem():
    """Typing a filter that matches nothing is a search miss, not "you have no
    models" — showing setup instructions there would be actively misleading."""
    palette = _FakeBufferPalette(
        container=None,
        models_source=lambda: [_model("llama3.2")],
        registered_probe=lambda: True,
        buffer_text="/model zzz",
    )
    out = _text(palette.render_models())

    assert "No model matches" in out
    assert "ollama pull" not in out
    assert "No models connected yet" not in out


def test_details_pane_describes_the_highlighted_model():
    palette = _FakeBufferPalette(
        container=None,
        models_source=lambda: [_model("llama3.2", "ollama")],
        buffer_text="/model",
    )
    out = _text(palette.render_details())

    assert "llama3.2" in out
    assert "ollama" in out
    assert "8,192 tokens" in out
    assert "tools" in out and "reasoning" in out


def test_list_pane_marks_the_highlighted_row():
    palette = _FakeBufferPalette(
        container=None,
        models_source=lambda: [_model("first"), _model("second")],
        buffer_text="/model",
    )
    first = _text(palette.render_models())
    assert "> first" in first

    palette.model.move("", 1)
    second = _text(palette.render_models())
    assert "> second" in second
    assert "> first" not in second


def test_details_pane_handles_an_empty_catalog_without_raising():
    palette = _palette([], registered=False)
    out = _text(palette.render_details())
    assert "HOW TO FIX" in out
    assert "/connect" in out


@pytest.mark.parametrize("registered", [True, False])
def test_empty_state_lines_fit_their_panes(registered):
    """Neither pane wraps, so an over-long line is silently clipped — which is
    how "ollama pull qwen2.5-coder:7b" would become "ollama pull qwen".

    Bounds are measured from a real layout render at 80 columns (the narrow
    case worth supporting): the list pane gets ~32 columns, the details pane
    ~41. Setup guidance is the one thing that must stay readable when nothing
    works yet, so it is held to that floor rather than to a wide terminal.
    """
    palette = _palette([], registered=registered)

    for line in _text(palette.render_models()).split("\n"):
        assert len(line) <= 32, f"list pane overflow ({len(line)}): {line!r}"

    for line in _text(palette.render_details()).split("\n"):
        assert len(line) <= 41, f"details pane overflow ({len(line)}): {line!r}"


# --- resilience ---------------------------------------------------------------


def test_connected_models_returns_empty_when_registries_are_missing():
    """Early startup / degraded runtime must not traceback over the prompt."""

    class _Broken:
        def get(self, _name):
            raise RuntimeError("not registered yet")

    assert connected_models(_Broken()) == []
    assert has_any_registered_model(_Broken()) is False


def test_connected_models_filters_out_unavailable_cloud_providers():
    local = _model("llama3.2", "ollama")
    reachable = _model("gpt-4o", "openai", is_local=False)
    unreachable = _model("claude", "anthropic", is_local=False)

    class _Container:
        def get(self, name):
            if name == "runtime.model_registry":
                return SimpleNamespace(list_all=lambda: [local, reachable, unreachable])
            return SimpleNamespace(
                check_provider_available=lambda pid: pid == "openai",
            )

    result = connected_models(_Container())
    assert [m.model_id for m in result] == ["llama3.2", "gpt-4o"]


def test_suppressed_palette_is_never_active():
    """An InlineFlow owning the prompt box must win over the palette."""
    palette = _palette([_model("a")], suppressed=lambda: True)
    assert palette.is_active() is False


# --- handover with the command palette ---------------------------------------
#
# Both palettes render into the *same* float rectangle, so "exactly one is
# active" is the property that keeps them from drawing on top of each other.


def _wired_pair():
    """A command palette + model palette wired the way repl.py wires them."""
    from velune.cli.command_palette import CommandPalette
    from velune.cli.slash_commands import SlashCommand

    commands = [
        SlashCommand(
            name=name, aliases=[], description="d", usage=f"/{name}", handler=None, category="c"
        )
        for name in ("model", "models", "help")
    ]

    class _MP(ModelPalette):
        def _buffer_text(self):
            return self.text

    class _CP(CommandPalette):
        def _buffer_text(self):
            return self.text

    model_palette = _MP(container=None, models_source=list, registered_probe=lambda: False)
    model_palette.text = ""
    command_palette = _CP(commands, suppressed=lambda: model_palette.is_active())
    command_palette.text = ""

    def drive(text):
        model_palette.text = command_palette.text = text
        return command_palette.is_active(), model_palette.is_active()

    return drive


@pytest.mark.parametrize(
    "text,command_active,model_active",
    [
        ("", False, False),
        ("/", True, False),
        ("/mo", True, False),  # still typing toward /model
        ("/mode", True, False),
        ("/model", False, True),  # the handover the user asked for
        ("/model ", False, True),
        ("/model llama", False, True),
        ("/models", False, True),  # listing command opens the picker too
        ("/models ", False, True),
        ("/help", True, False),
        ("/model use gpt-4o", False, False),  # subcommand: neither picker
    ],
)
def test_command_palette_yields_to_the_model_palette(text, command_active, model_active):
    drive = _wired_pair()
    assert drive(text) == (command_active, model_active)


def test_the_two_palettes_are_never_active_simultaneously():
    drive = _wired_pair()
    for text in ["", "/", "/m", "/mo", "/mod", "/mode", "/model", "/model ", "/model x", "/models"]:
        command_active, model_active = drive(text)
        assert not (command_active and model_active), f"both palettes active for {text!r}"


# --- "/models must not submit as a prompt, and must not error" ----------------


@pytest.mark.parametrize("text", ["/model", "/models"])
def test_enter_is_swallowed_when_there_is_nothing_to_select(text):
    """Pressing Enter on an empty catalog must not submit the line.

    Submitting it is what produced the red "No models available" error panel;
    the palette is already on screen explaining the same state with the
    commands that fix it, so the keypress has nothing useful to do.
    """
    submitted = []

    class _Buffer:
        def __init__(self):
            self.text = text
            self.cursor_position = len(text)

        def validate_and_handle(self):
            submitted.append(self.text)

    class _P(ModelPalette):
        def _buffer_text(self):
            return text

    palette = _P(container=None, models_source=list, registered_probe=lambda: False)
    bindings = _KeyBindingsSpy()
    palette.add_bindings(bindings)

    buffer = _Buffer()
    bindings.fire("enter", SimpleNamespace(current_buffer=buffer, app=_NoopApp()))

    assert submitted == [], "Enter must not submit while the catalog is empty"
    assert buffer.text == text, "Enter must not rewrite the prompt line"


@pytest.mark.parametrize("text", ["/model", "/models"])
def test_enter_selects_the_highlighted_model_when_one_exists(text):
    """With models present, Enter routes through the ordinary command path."""
    submitted = []

    class _Buffer:
        def __init__(self):
            self.text = text
            self.cursor_position = len(text)

        def validate_and_handle(self):
            submitted.append(self.text)

    class _P(ModelPalette):
        def _buffer_text(self):
            return text

    models = [_model("first"), _model("second")]
    palette = _P(container=None, models_source=lambda: models, registered_probe=lambda: True)
    bindings = _KeyBindingsSpy()
    palette.add_bindings(bindings)

    palette.model.move("", 1)  # highlight "second" with the down arrow
    buffer = _Buffer()
    bindings.fire("enter", SimpleNamespace(current_buffer=buffer, app=_NoopApp()))

    assert submitted == ["/model use second"]


def test_empty_catalog_is_not_rendered_as_an_error():
    """An empty catalog is a setup step on a fresh install, not a failure.

    `/models` used to submit and print a red "Error: No models available"
    panel; if the user dismisses the palette with Esc and submits anyway, the
    handler must still explain rather than alarm.
    """
    import inspect

    from velune.cli.handlers import model as handler

    for func in (handler.cmd_model, handler.cmd_models):
        src = inspect.getsource(func)
        assert "NoModelsAvailableError" not in src, f"{func.__name__} still renders an error panel"


def test_handler_and_palette_share_one_empty_state_wording():
    """Two surfaces describing the same state must not drift apart."""
    import inspect

    from velune.cli.handlers import model as handler
    from velune.cli.model_palette import empty_state_guidance

    assert "empty_state_guidance" in inspect.getsource(handler._print_no_models_guidance)

    headline, detail, remedies = empty_state_guidance(False)
    assert headline and detail and remedies
    assert any("ollama pull" in command for _label, command in remedies)


class _NoopApp:
    def invalidate(self):
        pass


class _KeyBindingsSpy:
    """Minimal stand-in for prompt_toolkit's KeyBindings.

    Captures the handlers ModelPalette registers so they can be fired directly,
    which is the only way to exercise the Enter/arrow contract without a live
    terminal.
    """

    def __init__(self):
        self.handlers: dict[str, list] = {}

    def add(self, *keys, **_kwargs):
        def decorator(func):
            self.handlers.setdefault(keys[0], []).append(func)
            return func

        return decorator

    def fire(self, key, event):
        for handler in self.handlers.get(key, []):
            handler(event)


# --- styling ------------------------------------------------------------------


def test_palette_is_cyan_blue_not_indigo():
    """The two palettes share a rectangle; hue is what distinguishes them."""
    from velune.cli import design

    title = MODEL_PALETTE_STYLES["model-palette.frame-title"]
    selected = MODEL_PALETTE_STYLES["model-palette.selected"]

    assert design.CYAN in title
    assert design.CYAN in selected
    # Must not fall back to the command palette's indigo.
    assert design.ACCENT not in title
    assert design.ACCENT_SOFT not in selected


def test_style_roles_mirror_the_command_palette():
    """Same surface, same roles — a missing key renders as an unstyled default,
    which is exactly how the two palettes would drift apart visually.

    The single intentional difference is the per-row role: a row here is a
    model, not a command.
    """
    from velune.cli.command_palette import PALETTE_STYLES

    command_roles = {key.split(".", 1)[1] for key in PALETTE_STYLES}
    model_roles = {key.split(".", 1)[1] for key in MODEL_PALETTE_STYLES}

    assert command_roles - model_roles == {"command"}
    assert model_roles - command_roles == {"model"}
    assert command_roles & model_roles == command_roles - {"command"}


def test_every_style_class_used_in_rendering_is_defined():
    """A typo'd class name renders as unstyled text rather than erroring, so
    this is the only thing that catches it."""
    import re
    from pathlib import Path

    src = Path("velune/cli/model_palette.py").read_text(encoding="utf-8")
    used = set(re.findall(r"class:(model-palette\.[a-z-]+)", src))
    assert used, "expected the renderer to reference style classes"
    assert used <= set(MODEL_PALETTE_STYLES), f"undefined: {used - set(MODEL_PALETTE_STYLES)}"
