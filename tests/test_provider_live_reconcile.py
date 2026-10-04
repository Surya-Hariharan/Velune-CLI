"""OpenAI / Anthropic / Gemini model catalogs are reconciled with the live model list.

A hardcoded catalog goes stale when a provider retires a model, and the Council
then picks a model that 404s mid-turn. These tests run offline against an
httpx mock transport standing in for each provider's /models endpoint.
"""

from __future__ import annotations

import httpx
import pytest

from velune.core.types.model import CapabilityLevel
from velune.providers.adapters import _live_catalog
from velune.providers.adapters._live_catalog import reconcile
from velune.providers.adapters.anthropic import ANTHROPIC_MODELS, discover_anthropic_models
from velune.providers.adapters.google import GEMINI_MODELS, discover_gemini_models
from velune.providers.adapters.openai import (
    OPENAI_MODELS,
    fetch_live_models,
    is_openai_chat_model,
    is_openai_reasoning_model,
    openai_context_window,
)
from velune.providers.discovery.openai import OpenAIDiscovery


@pytest.fixture
def serve(monkeypatch):
    """Route every httpx.AsyncClient used for discovery to a canned response."""
    state: dict = {"status": 200, "json": {}, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        return httpx.Response(state["status"], json=state["json"])

    real = httpx.AsyncClient
    monkeypatch.setattr(
        _live_catalog.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return state


def ids(models):
    return {m.model_id for m in models}


# --- OpenAI -----------------------------------------------------------------


async def test_openai_keeps_chat_models_and_drops_non_chat(serve, monkeypatch):
    serve["json"] = {
        "data": [
            {"id": "gpt-4o"},
            {"id": "gpt-5"},
            {"id": "o3-mini"},
            {"id": "gpt-image-1"},
            {"id": "gpt-4o-realtime-preview"},
            {"id": "text-embedding-3-small"},
            {"id": "whisper-1"},
            {"id": "gpt-4o-mini-tts"},
        ]
    }
    monkeypatch.setattr("velune.providers.discovery.openai.get_key", lambda _p: "sk-test")
    found = await OpenAIDiscovery().discover()
    # gpt-4o-mini is absent from the live list, so the curated entry is dropped.
    assert ids(found) == {"gpt-4o", "gpt-5", "o3-mini"}


async def test_openai_new_models_get_family_context_windows(serve, monkeypatch):
    serve["json"] = {"data": [{"id": "gpt-5"}, {"id": "gpt-4.1"}, {"id": "o3"}]}
    monkeypatch.setattr("velune.providers.discovery.openai.get_key", lambda _p: "sk-test")
    by_id = {m.model_id: m for m in await OpenAIDiscovery().discover()}
    assert by_id["gpt-5"].context_length == 400000
    assert by_id["gpt-4.1"].context_length == 1047576
    assert by_id["o3"].context_length == 200000


async def test_openai_falls_back_to_curated_on_http_error_or_no_key(serve, monkeypatch):
    serve["status"] = 401
    serve["json"] = {"error": {"message": "bad key"}}
    monkeypatch.setattr("velune.providers.discovery.openai.get_key", lambda _p: "sk-bad")
    assert ids(await OpenAIDiscovery().discover()) == ids(OPENAI_MODELS)
    monkeypatch.setattr("velune.providers.discovery.openai.get_key", lambda _p: None)
    assert await OpenAIDiscovery().discover() == []
    assert await fetch_live_models(None, "https://api.openai.com/v1") is None


def test_openai_classifiers():
    assert is_openai_chat_model("gpt-4o") and is_openai_chat_model("o4-mini")
    assert not is_openai_chat_model("gpt-image-1")
    assert not is_openai_chat_model("text-embedding-3-large")
    assert is_openai_reasoning_model("o3-mini") and is_openai_reasoning_model("gpt-5")
    assert not is_openai_reasoning_model("gpt-4o")
    assert not is_openai_reasoning_model("gpt-5-chat-latest")
    assert openai_context_window("unknown-model") == 16385


# --- Anthropic --------------------------------------------------------------


async def test_anthropic_drops_retired_and_adds_new_models(serve):
    serve["json"] = {
        "data": [
            {"id": "claude-opus-4-5", "max_input_tokens": 200000},
            {"id": "claude-sonnet-9-9", "max_input_tokens": 500000},
        ]
    }
    found = {m.model_id: m for m in await discover_anthropic_models("sk-ant-test")}
    assert set(found) == {"claude-opus-4-5", "claude-sonnet-9-9"}
    assert found["claude-sonnet-9-9"].context_length == 500000
    assert serve["requests"][0].headers["x-api-key"] == "sk-ant-test"
    assert "anthropic-version" in serve["requests"][0].headers


async def test_anthropic_new_opus_gets_expert_profile(serve):
    serve["json"] = {"data": [{"id": "claude-opus-9-0"}]}
    (model,) = await discover_anthropic_models("sk-ant-test")
    assert model.capabilities.coding == CapabilityLevel.EXPERT
    assert model.context_length == 200000


async def test_anthropic_falls_back_to_curated_when_listing_fails(serve):
    serve["status"] = 500
    assert ids(await discover_anthropic_models("sk-ant-test")) == ids(ANTHROPIC_MODELS)
    assert ids(await discover_anthropic_models(None)) == ids(ANTHROPIC_MODELS)


# --- Gemini -----------------------------------------------------------------


async def test_gemini_keeps_generate_content_chat_models_only(serve):
    gen = ["generateContent", "countTokens"]
    serve["json"] = {
        "models": [
            {
                "name": "models/gemini-2.5-pro",
                "inputTokenLimit": 1048576,
                "supportedGenerationMethods": gen,
            },
            {
                "name": "models/gemini-3-flash",
                "inputTokenLimit": 2000000,
                "supportedGenerationMethods": gen,
            },
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-2.5-flash-image", "supportedGenerationMethods": gen},
            {"name": "models/imagen-4", "supportedGenerationMethods": ["predict"]},
        ]
    }
    found = {m.model_id: m for m in await discover_gemini_models("g-key")}
    assert set(found) == {"gemini-2.5-pro", "gemini-3-flash"}  # retired curated entries dropped
    assert found["gemini-3-flash"].context_length == 2000000
    assert serve["requests"][0].headers["x-goog-api-key"] == "g-key"
    assert "key=" not in str(serve["requests"][0].url)  # the key never travels in the URL


async def test_gemini_falls_back_to_curated_offline(serve):
    serve["status"] = 503
    assert ids(await discover_gemini_models("g-key")) == ids(GEMINI_MODELS)
    assert not any("1.5" in mid for mid in ids(GEMINI_MODELS))


def test_reconcile_returns_curated_when_live_has_nothing_usable():
    result = reconcile(OPENAI_MODELS, {"whisper-1": None}, "openai", accept=is_openai_chat_model)
    assert ids(result) == ids(OPENAI_MODELS)
