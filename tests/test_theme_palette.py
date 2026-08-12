"""The theme registry, the theme palette, and instant switching.

Covers Part 2 of the theme rework: one canonical registry, a palette built on
the same architecture as the command/model palettes, ``/theme`` never reaching
inference, and a selection repainting every surface immediately.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from velune.cli import design, themes
from velune.cli.theme_palette import (
    ThemePalette,
    ThemePaletteModel,
    all_palette_styles,
    theme_palette_styles,
)


@pytest.fixture(autouse=True)
def _restore_default_theme():
    """Theme state is process-global (module attributes on `design`), so a test
    that switches it must not leak into the next one."""
    before = design.active_theme()
    colorblind = design.is_colorblind_mode()
    yield
    design.apply_theme(before)
    design.set_colorblind_mode(colorblind)


# --- Part 2B: the registry ----------------------------------------------------


def test_five_themes_are_registered():
    ids = [theme.id for theme in themes.list_themes()]
    assert ids == ["velune", "amber", "emerald", "crimson", "azure"]


def test_velune_is_the_default_and_comes_first():
    assert themes.DEFAULT_THEME_ID == "velune"
    assert themes.default_theme().id == "velune"
    assert themes.list_themes()[0].id == "velune"


def test_every_theme_defines_every_token():
    """A missing token would fall back to whatever the dataclass default was —
    there are none, so this is really a guard against a token being added to
    Theme without being given a value in all five."""
    hex_re = re.compile(r"^#[0-9a-f]{6}$")
    for theme in themes.list_themes():
        for field in (
            "primary",
            "secondary",
            "accent",
            "accent_soft",
            "background",
            "foreground",
            "foreground_dim",
            "muted",
            "border",
            "panel",
            "selection",
            "success",
            "warning",
            "error",
            "info",
        ):
            value = getattr(theme, field)
            assert hex_re.match(value), f"{theme.id}.{field} = {value!r} is not a hex colour"
        assert len(theme.gradient) == 3
        for stop in theme.gradient:
            assert hex_re.match(stop), f"{theme.id}.gradient stop {stop!r} is not a hex colour"


def test_each_theme_is_a_system_not_an_accent_swap():
    """The brief asks for coherent colour systems. If two themes shared their
    neutrals, one of them would be the other wearing a different accent."""
    backgrounds = {theme.background for theme in themes.list_themes()}
    panels = {theme.panel for theme in themes.list_themes()}
    assert len(backgrounds) == len(themes.list_themes())
    assert len(panels) == len(themes.list_themes())


def test_the_default_theme_preserves_the_existing_velune_identity():
    """An untouched install must render exactly as it did before themes
    existed — these are the hexes design.py carried."""
    velune = themes.get("velune")
    assert velune is not None
    assert velune.primary == "#818cf8"  # electric indigo
    assert velune.accent == "#38bdf8"  # cyan-blue
    assert velune.background == "#0a0a0a"


def test_resolve_falls_back_without_claiming_success():
    theme, ok = themes.resolve("no-such-theme")
    assert theme.id == "velune"
    assert ok is False


def test_resolve_accepts_none_and_empty_as_unset():
    for value in (None, ""):
        theme, ok = themes.resolve(value)
        assert theme.id == "velune"
        assert ok is True


# --- Part 2C: applying a theme ------------------------------------------------


def test_apply_theme_repoints_every_design_token():
    emerald = themes.get("emerald")
    assert emerald is not None
    design.apply_theme(emerald)

    assert design.active_theme_id() == "emerald"
    assert design.ACCENT == emerald.primary
    assert design.CYAN == emerald.accent
    assert design.BACKGROUND == emerald.background
    assert design.SURFACE == emerald.panel
    assert design.LIGHT_BG == emerald.selection
    assert design.OK == emerald.success
    assert design.WARN == emerald.warning
    assert design.DANGER == emerald.error
    assert design.WHITE == emerald.foreground
    assert design.FAINT == emerald.border


def test_apply_theme_updates_derived_aliases():
    """PINK/CONTROL/HIGHLIGHT and friends are aliases of the accent. If they
    kept their old value the UI would render half in each theme."""
    crimson = themes.get("crimson")
    assert crimson is not None
    design.apply_theme(crimson)

    for alias in ("PINK", "ACCENT_TEXT", "CONTROL", "HIGHLIGHT"):
        assert getattr(design, alias) == crimson.primary, alias
    assert design.SUCCESS == design.OK
    assert design.ERROR == design.DANGER


def test_gradient_follows_the_active_theme():
    azure = themes.get("azure")
    assert azure is not None
    design.apply_theme(azure)
    assert (design.GRAD_START, design.GRAD_MID, design.GRAD_END) == azure.gradient
    # gradient_hex reads the module globals, so it must move too.
    assert design.gradient_hex(0.0).lower() == azure.gradient[0].lower()


# --- Part 2C: every surface repaints -----------------------------------------


def _hexes(styles: dict[str, str]) -> set[str]:
    return set(re.findall(r"#[0-9a-f]{6}", " ".join(styles.values()).lower()))


def test_all_surfaces_change_together():
    """Style dicts used to be module-level and frozen at import, so a theme
    change repainted nothing. Each builder must re-read design at call time."""
    before = all_palette_styles()
    amber = themes.get("amber")
    assert amber is not None
    design.apply_theme(amber)
    after = all_palette_styles()

    assert before != after
    assert amber.primary.lower() in _hexes(after)
    assert "#818cf8" not in _hexes(after), "default indigo leaked into the amber sheet"


@pytest.mark.parametrize(
    "builder_path",
    [
        "velune.cli.command_palette:palette_styles",
        "velune.cli.model_palette:model_palette_styles",
        "velune.cli.model_switcher:model_switcher_styles",
        "velune.cli.statusbar:status_bar_styles",
        "velune.cli.home:home_styles",
        "velune.cli.theme_palette:theme_palette_styles",
    ],
)
def test_each_style_builder_is_theme_aware(builder_path):
    """Named individually so a regression names the surface that broke."""
    import importlib

    module_name, attr = builder_path.split(":")
    builder = getattr(importlib.import_module(module_name), attr)

    before = builder()
    emerald = themes.get("emerald")
    assert emerald is not None
    design.apply_theme(emerald)
    assert builder() != before, f"{builder_path} did not follow the theme change"


def test_style_builders_are_functions_not_frozen_dicts():
    """The specific defect being guarded: a module-level dict interpolates
    `design.*` once at import and can never change again."""
    import importlib

    for module_name, attr in (
        ("velune.cli.command_palette", "PALETTE_STYLES"),
        ("velune.cli.model_palette", "MODEL_PALETTE_STYLES"),
        ("velune.cli.model_switcher", "MODEL_SWITCHER_STYLES"),
        ("velune.cli.statusbar", "STATUS_BAR_STYLES"),
        ("velune.cli.home", "HOME_STYLES"),
    ):
        module = importlib.import_module(module_name)
        assert not hasattr(module, attr), f"{module_name}.{attr} is a frozen style dict"


# --- Part 2A: the palette reuses the existing architecture --------------------


def test_palette_opens_on_a_bare_theme_command():
    assert ThemePaletteModel.query_from_text("/theme") == ""
    assert ThemePaletteModel.query_from_text("  /theme  ") == ""


def test_palette_filters_on_trailing_text():
    assert ThemePaletteModel.query_from_text("/theme eme") == "eme"


@pytest.mark.parametrize("sub", ["colorblind", "cb", "motion", "animation", "status"])
def test_palette_stays_shut_for_accessibility_subcommands(sub):
    """Part 2D: these are display modifiers with their own grammar, not themes.
    Opening a picker over `/theme motion off` would submit the wrong thing."""
    assert ThemePaletteModel.query_from_text(f"/theme {sub}") is None
    assert ThemePaletteModel.query_from_text(f"/theme {sub} on") is None


def test_palette_ignores_other_commands():
    for text in ("/themes", "/themex", "/model", "hello", ""):
        assert ThemePaletteModel.query_from_text(text) is None


def test_filtering_matches_id_and_display_name():
    model = ThemePaletteModel()
    assert [t.id for t in model.matches("emerald")] == ["emerald"]
    assert [t.id for t in model.matches("amber")] == ["amber"]
    # Display name, case-insensitively — "Amber Gold" is what the row shows,
    # so typing what you can see has to work.
    assert [t.id for t in model.matches("Amber Gold")] == ["amber"]
    assert [t.id for t in model.matches("nonexistent")] == []
    assert len(model.matches("")) == 5


def test_arrow_navigation_wraps_like_the_other_palettes():
    model = ThemePaletteModel()
    assert model.selected("").id == "velune"  # noqa: E201 - alignment for readability
    model.move("", 1)
    assert model.selected("").id == "amber"
    model.move("", -1)
    assert model.selected("").id == "velune"
    model.move("", -1)
    assert model.selected("").id == "azure", "up from the first row should wrap to the last"


def test_palette_reuses_the_shared_float_geometry():
    """Part 2A: same container/frame/conditional structure as its siblings, so
    the three read as one surface changing contents."""
    from prompt_toolkit.layout.containers import ConditionalContainer

    from velune.cli.command_palette import CommandPalette

    palette = ThemePalette()
    container = palette.container()
    assert isinstance(container, ConditionalContainer)

    theme_src = Path("velune/cli/theme_palette.py").read_text(encoding="utf-8")
    command_src = Path("velune/cli/command_palette.py").read_text(encoding="utf-8")
    for marker in ("left=2,", "right=2,", "top=1,", "height=18,", "z_index="):
        assert marker in theme_src and marker in command_src, marker
    assert hasattr(CommandPalette, "attach") and hasattr(ThemePalette, "attach")


def test_every_style_class_used_in_rendering_is_defined():
    src = Path("velune/cli/theme_palette.py").read_text(encoding="utf-8")
    used = set(re.findall(r"class:(theme-palette\.[a-z-]+)", src))
    assert used, "expected the renderer to reference style classes"
    defined = set(theme_palette_styles())
    assert used <= defined, f"undefined: {used - defined}"
