"""Theme and credential persistence across a restart.

Part 3. Each test models a shutdown/startup boundary by writing config or
cache to a temp directory and then re-reading it through the *real* loader,
rather than asserting on in-memory state that a restart would discard —
in-memory state is exactly what the original bugs looked fine in.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import toml

from velune.cli import design, themes
from velune.cli.theme_state import (
    apply_startup_theme,
    configured_theme_id,
    persist_theme,
    resolve_startup_theme,
)
from velune.kernel.config import ConfigLoader, ThemeConfig, VeluneConfig


@pytest.fixture(autouse=True)
def _restore_default_theme():
    before = design.active_theme()
    yield
    design.apply_theme(before)


@pytest.fixture
def repl(tmp_path: Path):
    """A REPL stub exposing only what the settings persistence helpers use."""
    stub = MagicMock()
    stub.container.get.side_effect = lambda key: {
        "runtime.workspace": tmp_path,
        "runtime.config_path": None,
        "runtime.config": VeluneConfig(),
    }.get(key)
    stub._fullscreen_ui = None
    return stub


# --- Part 3A: the config carries the theme -----------------------------------


def test_theme_section_exists_in_the_config_schema():
    config = VeluneConfig()
    assert isinstance(config.theme, ThemeConfig)
    assert config.theme.active == "velune"


def test_theme_survives_a_round_trip_through_velune_toml(tmp_path: Path):
    """The regression proper: write it, parse it back with the real loader,
    and confirm the value is still there.

    The previous implementation wrote `[appearance] theme`, a section
    VeluneConfig does not declare. Being `extra="ignore"`, pydantic dropped it
    silently at parse time — so the value was written, discarded on load, and
    read by nothing.
    """
    config_file = tmp_path / "velune.toml"
    config_file.write_text(toml.dumps({"theme": {"active": "emerald"}}), encoding="utf-8")

    loaded = ConfigLoader(config_file).load()
    assert loaded.theme.active == "emerald"
    assert configured_theme_id(loaded) == "emerald"


def test_the_old_appearance_section_is_still_ignored(tmp_path: Path):
    """Proves the diagnosis rather than trusting it: a config written by the
    old settings TUI parses cleanly and yields *no* theme."""
    config_file = tmp_path / "velune.toml"
    config_file.write_text(toml.dumps({"appearance": {"theme": "nord"}}), encoding="utf-8")

    loaded = ConfigLoader(config_file).load()
    assert not hasattr(loaded, "appearance")
    assert loaded.theme.active == "velune"


def test_persist_theme_writes_the_theme_section(repl, tmp_path: Path):
    persist_theme(repl, "crimson")

    data = toml.load(tmp_path / "velune.toml")
    assert data["theme"]["active"] == "crimson"


def test_persist_then_reload_restores_the_same_theme(repl, tmp_path: Path):
    """End to end: select → (close) → (reopen) → same theme."""
    persist_theme(repl, "azure")

    reloaded = ConfigLoader(tmp_path / "velune.toml").load()
    apply_startup_theme(reloaded)

    assert design.active_theme_id() == "azure"


def test_persisting_a_theme_preserves_unrelated_settings(repl, tmp_path: Path):
    """A whole-file rewrite that dropped [providers] would take the user's
    default provider with it."""
    config_file = tmp_path / "velune.toml"
    config_file.write_text(
        toml.dumps(
            {"providers": {"default_provider": "groq"}, "display": {"colorblind_mode": True}}
        ),
        encoding="utf-8",
    )

    persist_theme(repl, "amber")

    data = toml.load(config_file)
    assert data["theme"]["active"] == "amber"
    assert data["providers"]["default_provider"] == "groq"
    assert data["display"]["colorblind_mode"] is True


# --- Part 3A: startup restoration --------------------------------------------


def test_startup_applies_the_configured_theme():
    config = VeluneConfig(theme={"active": "emerald"})
    problem = apply_startup_theme(config)

    assert problem is None
    assert design.active_theme_id() == "emerald"
    assert design.ACCENT == themes.get("emerald").primary


def test_startup_uses_the_default_when_nothing_is_configured():
    assert apply_startup_theme(VeluneConfig()) is None
    assert design.active_theme_id() == "velune"


def test_an_invalid_theme_id_falls_back_and_is_reported():
    config = VeluneConfig(theme={"active": "chartreuse"})
    theme, problem = resolve_startup_theme(config)

    assert theme.id == "velune"
    assert problem is not None and "chartreuse" in problem


def test_an_invalid_theme_id_does_not_corrupt_the_config(tmp_path: Path):
    """Reported, never rewritten — silently 'correcting' the value destroys
    the evidence of the user's typo."""
    config_file = tmp_path / "velune.toml"
    original = toml.dumps({"theme": {"active": "chartreuse"}})
    config_file.write_text(original, encoding="utf-8")

    apply_startup_theme(ConfigLoader(config_file).load())

    assert config_file.read_text(encoding="utf-8") == original


def test_startup_theme_is_applied_before_any_ui_is_built():
    """Guards the no-flash requirement: build_runtime must apply the theme in
    the same block that already applied the accessibility modifiers, which
    runs before any Console or prompt_toolkit style exists."""
    src = Path("velune/core/runtime.py").read_text(encoding="utf-8")
    assert "apply_startup_theme" in src
    assert src.index("apply_startup_theme(config)") < src.index("container = ServiceContainer()")


# --- Part 2D: accessibility settings are independent of the theme ------------


def test_colorblind_mode_survives_a_theme_change():
    design.set_colorblind_mode(True)
    okabe_ito_green = design.OK

    design.apply_theme(themes.get("amber"))

    assert design.is_colorblind_mode() is True
    assert design.OK == okabe_ito_green, "theme change reinstated the theme's own severity colours"
    design.set_colorblind_mode(False)


def test_turning_colorblind_off_restores_the_current_theme_not_the_startup_one():
    """The severity trio used to be snapshotted at import, so turning the
    modifier off after a theme change restored the *previous* theme's colours."""
    crimson = themes.get("crimson")
    design.apply_theme(crimson)
    design.set_colorblind_mode(True)
    design.set_colorblind_mode(False)

    assert design.OK == crimson.success
    assert design.WARN == crimson.warning
    assert design.DANGER == crimson.error


def test_theme_and_accessibility_live_in_separate_config_sections():
    config = VeluneConfig()
    assert hasattr(config.theme, "active")
    assert hasattr(config.display, "colorblind_mode")
    assert hasattr(config.display, "reduced_motion")
    assert not hasattr(config.theme, "colorblind_mode")


# --- Part 3E: safe persistence ------------------------------------------------


def test_config_write_is_atomic(repl, tmp_path: Path, monkeypatch):
    """A crash mid-write must leave the last known-good config intact rather
    than a truncated file that takes every unrelated setting with it."""
    from velune.cli.handlers import settings as settings_mod

    config_file = tmp_path / "velune.toml"
    original = toml.dumps({"providers": {"default_provider": "groq"}})
    config_file.write_text(original, encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    # `os` is imported inside save_setting_to_toml, so the patch has to land
    # on the os module itself rather than a module-level name.
    monkeypatch.setattr("os.replace", _boom)
    settings_mod.save_setting_to_toml(repl, "theme", "active", "emerald")

    assert config_file.read_text(encoding="utf-8") == original


def test_failed_write_leaves_no_temp_files_behind(repl, tmp_path: Path, monkeypatch):
    from velune.cli.handlers import settings as settings_mod

    (tmp_path / "velune.toml").write_text("[providers]\n", encoding="utf-8")
    monkeypatch.setattr("os.replace", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    settings_mod.save_setting_to_toml(repl, "theme", "active", "amber")

    assert list(tmp_path.glob("*.tmp")) == []


def test_an_unparseable_config_is_never_overwritten(repl, tmp_path: Path):
    """Treating a parse failure as "start from {}" would silently discard a
    config we merely failed to read."""
    config_file = tmp_path / "velune.toml"
    garbage = "this is not = valid toml ][\n"
    config_file.write_text(garbage, encoding="utf-8")

    from velune.cli.handlers.settings import save_setting_to_toml

    save_setting_to_toml(repl, "theme", "active", "emerald")

    assert config_file.read_text(encoding="utf-8") == garbage


# --- Part 3B/3D: providers survive a restart ---------------------------------


def test_discovered_models_are_persisted_not_just_registered(tmp_path: Path):
    """The real "my API key was lost" bug: /connect registered discovered
    models in memory only, so the next launch had a cache that had never heard
    of the provider and reported it unconfigured."""
    from velune.core.types.model import ModelCapabilityProfile, ModelDescriptor
    from velune.models.registry import ModelCapabilityRegistry
    from velune.models.registry_cache import ModelRegistryCache

    cache_path = tmp_path / "registry.json"
    registry = ModelCapabilityRegistry()
    registry._registry_cache = ModelRegistryCache(cache_path)

    registry.register(
        ModelDescriptor(
            model_id="llama-3.3-70b",
            provider_id="groq",
            display_name="Llama 3.3 70B",
            context_length=8192,
            capabilities=ModelCapabilityProfile(),
            is_local=False,
        )
    )
    assert registry.persist() is True

    restored = [m.model_id for m in ModelRegistryCache(cache_path).load()]
    assert "llama-3.3-70b" in restored


def test_cloud_models_are_not_dropped_merely_for_being_stale(tmp_path: Path):
    """Staleness is a refresh hint, not a delete signal. Dropping aged cloud
    entries on load is what made a still-valid credential look disconnected."""
    from velune.models.registry_cache import ModelRegistryCache

    cache_path = tmp_path / "registry.json"
    cache_path.write_text(
        json.dumps(
            {
                # Two days old: far past the 60-minute cloud TTL.
                "saved_at": time.time() - 172_800,
                "models": [
                    {
                        "model_id": "llama-3.3-70b",
                        "provider_id": "groq",
                        "display_name": "llama-3.3-70b",
                        "context_length": 8192,
                        "is_local": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    cache = ModelRegistryCache(cache_path)
    assert [m.model_id for m in cache.load()] == ["llama-3.3-70b"]
    # Callers that genuinely want only fresh rows can still ask.
    assert cache.load(drop_stale=True) == []
