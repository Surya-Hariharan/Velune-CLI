"""`velune models use` — the non-interactive default-model switch.

Verifies the Switch verb persists the choice the same way the REPL's
``/model use`` does (active_model.json + providers.default_provider) and that it
refuses unknown models rather than silently pointing the runtime at nothing.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer

from velune.cli import model_prefs
from velune.cli.commands import models as models_cmd_mod
from velune.cli.context import CLIContext


class _FakeDescriptor:
    def __init__(self, model_id: str, provider_id: str):
        self.model_id = model_id
        self.provider_id = provider_id
        self.context_length = 32000
        self.is_local = True


class _FakeRegistry:
    def __init__(self, known: dict[str, _FakeDescriptor]):
        self._known = known

    def get(self, model_id: str, provider_id: str | None = None):
        return self._known.get(model_id)


class _FakeContainer:
    def __init__(self, registry):
        self._registry = registry

    def get(self, key: str):
        if key == "runtime.model_registry":
            return self._registry
        return None


def _ctx(registry, workspace):
    runtime = SimpleNamespace(container=_FakeContainer(registry))
    cli_context = CLIContext(
        workspace=workspace,
        config_path=None,
        verbose=False,
        runtime=runtime,
        json_mode=False,
    )
    return SimpleNamespace(obj=cli_context)


@pytest.fixture(autouse=True)
def _isolate_prefs(tmp_path, monkeypatch):
    """Redirect the active-model preference file into the test's tmp dir."""
    monkeypatch.setattr(model_prefs, "DEFAULT_PREFS_PATH", tmp_path / "active_model.json")


def test_use_sets_default_model(tmp_path):
    desc = _FakeDescriptor("qwen2.5-coder:7b", "ollama")
    registry = _FakeRegistry({desc.model_id: desc})
    ctx = _ctx(registry, tmp_path)

    models_cmd_mod.models_use(ctx, model_id="qwen2.5-coder:7b")

    pref = model_prefs.load_active_model()
    assert pref is not None
    assert pref.model_id == "qwen2.5-coder:7b"
    assert pref.provider_id == "ollama"

    # Provider default is persisted into the workspace velune.toml.
    import toml

    data = toml.load(tmp_path / "velune.toml")
    assert data["providers"]["default_provider"] == "ollama"


def test_use_unknown_model_exits_nonzero(tmp_path):
    registry = _FakeRegistry({})
    ctx = _ctx(registry, tmp_path)

    with pytest.raises(typer.Exit) as exc:
        models_cmd_mod.models_use(ctx, model_id="ghost-model")
    assert exc.value.exit_code == 1
    # Nothing should have been persisted.
    assert model_prefs.load_active_model() is None


def test_use_no_arg_shows_current(tmp_path, capsys):
    desc = _FakeDescriptor("llama3.2:3b", "ollama")
    registry = _FakeRegistry({desc.model_id: desc})
    ctx = _ctx(registry, tmp_path)

    models_cmd_mod.models_use(ctx, model_id="llama3.2:3b")
    capsys.readouterr()  # drop the "set" output

    # No argument → report current default without changing it.
    models_cmd_mod.models_use(ctx, model_id=None)
    out = capsys.readouterr().out
    assert "llama3.2:3b" in out
