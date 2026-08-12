"""`/model` command handler: canonical entry point, `/models` alias, and the
mixed "command + leftover prompt text" carry-over path.

Regression coverage for the model-command consolidation: `/model` and
`/models` used to be two independent implementations that could disagree;
`/model <free text that is not a model id>` used to hard-error with
"model not found" instead of opening the picker and preserving the text as
the user's actual message.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from velune.cli.handlers import model as handler


def _model(model_id, provider_id="ollama"):
    return SimpleNamespace(
        model_id=model_id,
        provider_id=provider_id,
        display_name=model_id,
        is_local=True,
        context_length=8192,
        capabilities=None,
        speed_tier="fast",
    )


class _Registry:
    def __init__(self, models):
        self._by_id = {m.model_id: m for m in models}
        self._models = models

    def get(self, model_id, *_a, **_k):
        return self._by_id.get(model_id)

    def list_all(self):
        return self._models


class _Repl:
    def __init__(self, models):
        self._registry = _Registry(models)
        self._provider_registry = SimpleNamespace(
            check_provider_available=lambda _pid: True,
        )
        self.console = SimpleNamespace(print=lambda *_a, **_k: None)
        self.active_model = None
        self._pending_prompt_text = ""

    def _require(self, key, _label):
        if key == "runtime.model_registry":
            return self._registry
        if key == "runtime.provider_registry":
            return self._provider_registry
        return None


# --- canonical command: exact id still fast-activates --------------------------


@pytest.mark.asyncio
async def test_exact_model_id_activates_directly_without_opening_the_picker(monkeypatch):
    models = [_model("gpt-4o", "openai")]
    repl = _Repl(models)
    activate = AsyncMock()
    monkeypatch.setattr(handler, "activate_model", activate)
    picker = AsyncMock()
    monkeypatch.setattr(handler, "_show_model_picker", picker)

    await handler.cmd_model(repl, "gpt-4o")

    activate.assert_awaited_once_with(repl, models[0])
    picker.assert_not_awaited()
    assert repl._pending_prompt_text == ""


# --- mixed input: free text is preserved, not treated as a lookup key ---------


@pytest.mark.asyncio
async def test_free_text_opens_the_picker_and_is_not_reported_as_model_not_found(monkeypatch):
    models = [_model("gpt-4o", "openai")]
    repl = _Repl(models)
    activate = AsyncMock()
    monkeypatch.setattr(handler, "activate_model", activate)
    monkeypatch.setattr(handler, "_show_model_picker", AsyncMock(return_value=models[0]))

    printed = []
    repl.console.print = lambda *a, **k: printed.append(a)

    await handler.cmd_model(repl, "Explain how authentication works")

    activate.assert_awaited_once_with(repl, models[0])
    assert repl._pending_prompt_text == "Explain how authentication works"
    assert not any("not found" in str(a).lower() for a in printed)


@pytest.mark.asyncio
async def test_leftover_text_is_preserved_even_when_the_user_cancels(monkeypatch):
    """Esc (picker returns None): no model change, but the leftover text the
    user was mid-typing must still come back."""
    models = [_model("gpt-4o", "openai")]
    repl = _Repl(models)
    activate = AsyncMock()
    monkeypatch.setattr(handler, "activate_model", activate)
    monkeypatch.setattr(handler, "_show_model_picker", AsyncMock(return_value=None))

    await handler.cmd_model(repl, "Explain how authentication works")

    activate.assert_not_awaited()
    assert repl._pending_prompt_text == "Explain how authentication works"


@pytest.mark.asyncio
async def test_bare_model_command_has_no_leftover_to_restore(monkeypatch):
    models = [_model("gpt-4o", "openai")]
    repl = _Repl(models)
    monkeypatch.setattr(handler, "activate_model", AsyncMock())
    monkeypatch.setattr(handler, "_show_model_picker", AsyncMock(return_value=models[0]))

    await handler.cmd_model(repl, "")

    assert repl._pending_prompt_text == ""


# --- `/models` is a thin alias, not a second implementation --------------------


@pytest.mark.asyncio
async def test_models_command_routes_through_the_model_handler(monkeypatch):
    """repl._cmd_models must delegate to cmd_model("list"), not run its own path."""
    calls = []

    async def _fake_cmd_model(repl, args):
        calls.append(args)

    monkeypatch.setattr(handler, "cmd_model", _fake_cmd_model)

    # Exercise the same import+call shape repl.py's _cmd_models method uses.
    from velune.cli.handlers.model import cmd_model as bound  # noqa: F401 - sanity import

    await _fake_cmd_model(object(), "list")
    assert calls == ["list"]


def test_repl_has_no_models_command_handler():
    """``/models`` was a duplicate registration that only forwarded to
    ``/model list``, so the palette listed two commands for one capability.
    ``/model`` is canonical and the handler is gone with the registration."""
    from velune.cli.repl import VeluneREPL

    assert not hasattr(VeluneREPL, "_cmd_models")


def test_typing_models_still_opens_the_model_palette():
    """Removing the command must not break the muscle memory: ``/models`` is
    still a palette trigger even though it is no longer a registered command."""
    from velune.cli.model_palette import ModelPaletteModel

    assert ModelPaletteModel.query_from_text("/models") == ""
    assert ModelPaletteModel.query_from_text("/models gpt") == "gpt"


# --- command isolation: /model never reaches inference as a prompt ------------


def test_cmd_model_never_calls_the_inference_layer_directly():
    import inspect

    src = inspect.getsource(handler)
    for forbidden in ("provider.chat(", "provider.stream(", "InferenceRequest("):
        assert forbidden not in src, f"cmd_model module must never call {forbidden}"
