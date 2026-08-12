"""The slash-command registry has exactly one command per capability.

Guards the Part 1 / Part 4 cleanup: ``/provider``, ``/providers`` and
``/models`` were removed because each duplicated a command that already
existed, and a duplicate is invisible in review — it only shows up as two
plausible-looking rows in the command palette.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from velune.cli.slash_commands import SlashCommandRegistry


@pytest.fixture
def registry() -> SlashCommandRegistry:
    """A registry built exactly as the REPL builds it at startup.

    The REPL instance is a MagicMock: every command's handler is one of its
    ``_cmd_*`` attributes, and none of them is called here — this suite only
    inspects registration.
    """
    from velune.cli.slash_dispatcher import build_slash_registry

    repl = MagicMock()
    return build_slash_registry(repl)


def _all_names_and_aliases(registry: SlashCommandRegistry) -> set[str]:
    names: set[str] = set()
    for command in registry.all_unique():
        names.add(command.name)
        names.update(command.aliases or ())
    return names


# --- Part 1: provider command removal ----------------------------------------


@pytest.mark.parametrize("removed", ["provider", "providers", "prov"])
def test_provider_commands_are_not_registered(registry, removed):
    assert removed not in _all_names_and_aliases(registry)


@pytest.mark.parametrize("removed", ["provider", "providers", "prov"])
def test_provider_commands_do_not_resolve(registry, removed):
    """Not merely absent from the listing — genuinely unresolvable, so nothing
    can still dispatch to them through an alias lookup."""
    assert registry.get(removed) is None


def test_provider_commands_are_absent_from_the_command_palette(registry):
    from velune.cli.command_palette import CommandPaletteModel

    model = CommandPaletteModel(registry.all_unique())
    for query in ("provider", "providers", "prov"):
        names = {match.command.name for match in model.matches(query)}
        assert "providers" not in names
        assert "provider" not in names


def test_the_repl_has_no_providers_handler():
    from velune.cli.repl import VeluneREPL

    assert not hasattr(VeluneREPL, "_cmd_providers")


# --- Part 1: /connect is the canonical provider entry point -------------------


def test_connect_is_registered_and_canonical(registry):
    command = registry.get("connect")
    assert command is not None
    assert command.name == "connect"


@pytest.mark.parametrize("alias", ["login", "auth"])
def test_connect_keeps_its_aliases(registry, alias):
    command = registry.get(alias)
    assert command is not None and command.name == "connect"


def test_only_one_command_lives_in_the_providers_category(registry):
    providers = [c for c in registry.all_unique() if c.category == "Providers"]
    assert [c.name for c in providers] == ["connect"]


def test_searching_for_provider_words_finds_connect(registry):
    """The old command owned the discoverable vocabulary ("api key",
    "credentials", a provider's name). Removing it must not make that
    vocabulary dead — it moved onto /connect's search terms."""
    from velune.cli.command_palette import CommandPaletteModel

    model = CommandPaletteModel(registry.all_unique())
    for query in ("provider", "api key", "credentials", "anthropic", "groq"):
        names = {match.command.name for match in model.matches(query)}
        assert "connect" in names, f"{query!r} should surface /connect"


# --- Part 4: /model is canonical, /models is gone -----------------------------


def test_model_is_registered(registry):
    command = registry.get("model")
    assert command is not None and command.name == "model"


def test_models_command_is_not_registered(registry):
    assert "models" not in _all_names_and_aliases(registry)
    assert registry.get("models") is None


def test_no_duplicate_names_or_aliases_anywhere(registry):
    """A name colliding with another command's alias silently shadows one of
    them at dispatch time; nothing else in the codebase checks for it."""
    seen: dict[str, str] = {}
    for command in registry.all_unique():
        for token in (command.name, *(command.aliases or ())):
            assert token not in seen, (
                f"{token!r} is claimed by both /{seen[token]} and /{command.name}"
            )
            seen[token] = command.name


# --- Part 4: the palette reflects the registry --------------------------------


def test_theme_is_registered(registry):
    command = registry.get("theme")
    assert command is not None and command.name == "theme"


def test_palette_lists_the_canonical_trio(registry):
    from velune.cli.command_palette import CommandPaletteModel

    model = CommandPaletteModel(registry.all_unique())
    listed = {match.command.name for match in model.matches("")}
    assert {"connect", "model", "theme"} <= listed
    assert not listed & {"providers", "provider", "models"}
