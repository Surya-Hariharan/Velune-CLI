"""Tests for cross-platform Ollama model location discovery/registration."""

from __future__ import annotations

from pathlib import Path

from velune.providers.ollama_locations import OllamaLocationRegistry
from velune.providers.ollama_store import OllamaModelStore


def _make_store_root(root: Path) -> Path:
    """Build a minimal valid Ollama store layout (manifests/ + blobs/)."""
    (root / "manifests").mkdir(parents=True)
    (root / "blobs").mkdir(parents=True)
    return root


def test_is_valid_root_requires_both_dirs(tmp_path):
    assert not OllamaModelStore.is_valid_root(tmp_path)
    (tmp_path / "manifests").mkdir()
    assert not OllamaModelStore.is_valid_root(tmp_path)
    (tmp_path / "blobs").mkdir()
    assert OllamaModelStore.is_valid_root(tmp_path)


def test_add_rejects_missing_or_invalid_path(tmp_path):
    reg = OllamaLocationRegistry(path=tmp_path / "locations.json")

    missing = reg.add(tmp_path / "does-not-exist")
    assert not missing.ok

    not_a_store = tmp_path / "plain_dir"
    not_a_store.mkdir()
    result = reg.add(not_a_store)
    assert not result.ok
    assert "Not an Ollama model store" in result.message


def test_add_is_idempotent_and_persists(tmp_path):
    store_root = _make_store_root(tmp_path / "external_drive" / "ollama_models")
    reg = OllamaLocationRegistry(path=tmp_path / "locations.json")

    first = reg.add(store_root, label="External SSD")
    assert first.ok
    second = reg.add(store_root, label="External SSD")
    assert second.ok
    assert "Already registered" in second.message

    loaded = reg.load()
    assert len(loaded) == 1
    assert loaded[0].label == "External SSD"

    # Persisted to disk, not just in-memory — a fresh registry instance sees it.
    reloaded = OllamaLocationRegistry(path=tmp_path / "locations.json").load()
    assert len(reloaded) == 1
    assert Path(reloaded[0].path) == store_root


def test_remove_location(tmp_path):
    store_root = _make_store_root(tmp_path / "models")
    reg = OllamaLocationRegistry(path=tmp_path / "locations.json")
    reg.add(store_root)

    assert reg.remove(store_root) is True
    assert reg.load() == []
    # Removing again reports no match rather than erroring.
    assert reg.remove(store_root) is False


def test_resolve_roots_orders_registered_before_env_before_defaults(tmp_path, monkeypatch):
    registered_root = _make_store_root(tmp_path / "registered")
    env_root = tmp_path / "env_models"
    env_root.mkdir()

    monkeypatch.setenv("OLLAMA_MODELS", str(env_root))
    reg = OllamaLocationRegistry(path=tmp_path / "locations.json")
    reg.add(registered_root)

    roots = reg.resolve_roots()
    sources = [r.source for r in roots]

    assert sources[0] == "registered"
    assert "env:OLLAMA_MODELS" in sources
    assert sources.index("registered") < sources.index("env:OLLAMA_MODELS")
    assert any(s == "default" for s in sources)


def test_disconnected_registered_root_is_flagged_not_dropped(tmp_path):
    """A registered external drive that's unplugged must still show up, flagged."""
    store_root = _make_store_root(tmp_path / "usb_drive" / "models")
    reg = OllamaLocationRegistry(path=tmp_path / "locations.json")
    reg.add(store_root)

    # Simulate the drive disappearing.
    import shutil

    shutil.rmtree(tmp_path / "usb_drive")

    roots = reg.resolve_roots()
    registered = [r for r in roots if r.source == "registered"]
    assert len(registered) == 1
    assert registered[0].disconnected is True
    assert registered[0].exists is False

    disconnected = reg.disconnected()
    assert len(disconnected) == 1


def test_active_stores_skips_unreachable_and_invalid_roots(tmp_path, monkeypatch):
    from velune.providers import ollama_locations as mod

    # Isolate from this machine's own real Ollama install/env, which would
    # otherwise leak extra (real) stores into the result.
    monkeypatch.delenv("OLLAMA_MODELS", raising=False)
    monkeypatch.setattr(mod, "_default_roots", lambda: [])

    valid_root = _make_store_root(tmp_path / "valid")
    reg = OllamaLocationRegistry(path=tmp_path / "locations.json")
    reg.add(valid_root)

    stores = reg.active_stores()
    assert len(stores) == 1
    assert stores[0].root == valid_root


def test_default_roots_are_platform_appropriate(monkeypatch):
    """Windows machines get USERPROFILE/LOCALAPPDATA roots; others don't."""
    from velune.providers import ollama_locations as mod

    monkeypatch.setattr(mod.platform, "system", lambda: "Windows")
    monkeypatch.setenv("USERPROFILE", "D:\\Users\\someone")
    monkeypatch.setenv("LOCALAPPDATA", "D:\\Users\\someone\\AppData\\Local")
    windows_roots = mod._default_roots()
    assert any("someone" in str(p) for p in windows_roots)
    assert any(str(p).endswith("Ollama\\models") or str(p).endswith("Ollama/models") for p in windows_roots)

    monkeypatch.setattr(mod.platform, "system", lambda: "Linux")
    linux_roots = mod._default_roots()
    assert any(p.as_posix() == "/usr/share/ollama/.ollama/models" for p in linux_roots)


def test_key_normalizes_path_case_and_trailing_slash(tmp_path):
    a = OllamaLocationRegistry._key(str(tmp_path) + "/")
    b = OllamaLocationRegistry._key(str(tmp_path))
    assert a == b
