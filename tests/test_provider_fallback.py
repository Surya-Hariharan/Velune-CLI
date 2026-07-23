"""Cross-provider fallback candidate selection (VeluneREPL._next_fallback_candidate).

Previously a provider outage during ordinary chat surfaced as a raw error —
the "chat path" had none of Council's (unused) fallback machinery. This pins
the candidate-selection logic the REPL's turn loop (velune/cli/repl.py,
_handle_prompt) uses to pick the next configured provider to retry with.
"""

from __future__ import annotations

from types import SimpleNamespace

from velune.cli.repl import VeluneREPL
from velune.core.types.model import ModelDescriptor


def _model(model_id: str, provider_id: str) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        provider_id=provider_id,
        display_name=model_id,
        context_length=8192,
        capabilities={},
    )


class _FakeModelRegistry:
    def __init__(self, models: list[ModelDescriptor]) -> None:
        self._models = models

    def list_all(self) -> list[ModelDescriptor]:
        return self._models


class _FakeProviderRegistry:
    def __init__(self, configured: set[str]) -> None:
        self._configured = configured

    def get(self, provider_id: str):
        return SimpleNamespace(provider_id=provider_id) if provider_id in self._configured else None


class _FakeContainer:
    def __init__(self, services: dict) -> None:
        self._services = services

    def get(self, key: str):
        return self._services[key]


def _repl_with(models: list[ModelDescriptor], configured_providers: set[str]):
    container = _FakeContainer(
        {
            "runtime.model_registry": _FakeModelRegistry(models),
            "runtime.provider_registry": _FakeProviderRegistry(configured_providers),
        }
    )
    return SimpleNamespace(container=container)


def test_skips_already_tried_providers():
    fake = _repl_with(
        [_model("m1", "anthropic"), _model("m2", "openai")],
        configured_providers={"anthropic", "openai"},
    )
    result = VeluneREPL._next_fallback_candidate(fake, {"anthropic"})
    assert result is not None
    model, provider = result
    assert model.provider_id == "openai"
    assert provider.provider_id == "openai"


def test_skips_providers_with_no_configured_adapter():
    fake = _repl_with(
        [_model("m1", "openai")],  # not configured — no key
        configured_providers=set(),
    )
    assert VeluneREPL._next_fallback_candidate(fake, set()) is None


def test_returns_none_once_every_provider_tried():
    fake = _repl_with(
        [_model("m1", "anthropic"), _model("m2", "openai")],
        configured_providers={"anthropic", "openai"},
    )
    assert VeluneREPL._next_fallback_candidate(fake, {"anthropic", "openai"}) is None


def test_returns_none_when_registries_unavailable():
    fake = SimpleNamespace(container=_FakeContainer({}))
    assert VeluneREPL._next_fallback_candidate(fake, set()) is None


def test_follows_model_registry_order():
    fake = _repl_with(
        [_model("m1", "mistral"), _model("m2", "anthropic"), _model("m3", "openai")],
        configured_providers={"mistral", "anthropic", "openai"},
    )
    model, provider = VeluneREPL._next_fallback_candidate(fake, set())
    assert model.provider_id == "mistral"  # first in registry order, not tried yet
