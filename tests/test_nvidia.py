"""NVIDIA NIM: key verification, model identity, filtering and error typing.

Every request goes to an in-process mock transport (``tests/http_fakes.py``);
nothing is sent to NVIDIA.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests.http_fakes import install_mock_transport, isolate_keystore, time_out
from velune.core.errors.provider import (
    InferenceError,
    ModelNotFoundError,
    ProviderAuthenticationError,
    ProviderPermissionError,
)
from velune.core.types.inference import InferenceRequest
from velune.providers.adapters.nvidia import NVIDIAProvider, is_nim_chat_model, pick_probe_model
from velune.providers.validation import ValidationStatus, validate_provider

KEY = "nvapi-test-0000000000000000000000"
LISTING = {
    "data": [
        {"id": "meta/llama-3.1-8b-instruct"},
        {"id": "meta/llama-3.3-70b-instruct"},
        {"id": "nvidia/nv-embedqa-e5-v5"},
        {"id": "nvidia/llama-3.2-nv-rerankqa-1b-v2"},
        {"id": "deepseek-ai/deepseek-r1"},
    ]
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    isolate_keystore(monkeypatch, tmp_path)


def _nim(chat_status: int = 200, *, list_status: int = 200, chat_body: dict | None = None):
    """A fake NIM API whose /models ignores the key — exactly like the real one."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(list_status, json=LISTING if list_status == 200 else {})
        if request.url.path.endswith("/chat/completions"):
            body = chat_body or {
                "choices": [{"message": {"content": "p"}, "finish_reason": "length"}]
            }
            if chat_status != 200:
                body = {"error": {"message": "nope"}}
            return httpx.Response(chat_status, json=body)
        return httpx.Response(404)

    return handler


# ---------------------------------------------------------------------------
# Key verification
# ---------------------------------------------------------------------------


async def test_listing_alone_does_not_verify_the_key(monkeypatch):
    """/models answers 200 for any key; only the 1-token completion proves it."""
    seen = install_mock_transport(monkeypatch, _nim(chat_status=401))
    result = await validate_provider("nvidia", KEY)
    assert result.status is ValidationStatus.INVALID_KEY
    assert result.rejects_key
    chat = [r for r in seen if r.url.path.endswith("/chat/completions")]
    assert chat, "the key must be exercised by a completion, not just a listing"
    payload = json.loads(chat[0].content)
    assert payload["max_tokens"] == 1
    assert payload["model"] == "meta/llama-3.1-8b-instruct"


async def test_valid_key_is_verified_and_lists_every_model(monkeypatch):
    install_mock_transport(monkeypatch, _nim())
    result = await validate_provider("nvidia", KEY)
    assert result.ok
    # The full listing, not the first 20.
    assert len(result.models) == len(LISTING["data"])


async def test_unavailable_probe_model_is_unconfirmed_not_invalid(monkeypatch):
    install_mock_transport(monkeypatch, _nim(chat_status=404))
    result = await validate_provider("nvidia", KEY)
    assert result.status is ValidationStatus.UNCONFIRMED
    assert result.inconclusive and not result.rejects_key


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, ValidationStatus.PERMISSION_DENIED),
        (429, ValidationStatus.RATE_LIMITED),
        (503, ValidationStatus.PROVIDER_UNAVAILABLE),
    ],
)
async def test_non_auth_failures_never_claim_the_key_is_bad(monkeypatch, status, expected):
    install_mock_transport(monkeypatch, _nim(chat_status=status))
    result = await validate_provider("nvidia", KEY)
    assert result.status is expected
    assert not result.rejects_key


async def test_timeout_is_reported_as_timeout(monkeypatch):
    install_mock_transport(monkeypatch, time_out)
    result = await validate_provider("nvidia", KEY)
    assert result.status is ValidationStatus.TIMEOUT
    assert KEY not in result.message


async def test_authenticate_uses_the_completion_check(monkeypatch):
    install_mock_transport(monkeypatch, _nim(chat_status=401))
    with pytest.raises(ProviderAuthenticationError):
        await NVIDIAProvider(api_key=KEY).authenticate()

    install_mock_transport(monkeypatch, _nim())
    await NVIDIAProvider(api_key=KEY).authenticate()


# ---------------------------------------------------------------------------
# Model listing: chat models only, under the "nvidia" provider id
# ---------------------------------------------------------------------------


def test_non_chat_models_are_filtered():
    assert is_nim_chat_model("meta/llama-3.3-70b-instruct")
    assert is_nim_chat_model("deepseek-ai/deepseek-r1")
    assert not is_nim_chat_model("nvidia/nv-embedqa-e5-v5")
    assert not is_nim_chat_model("nvidia/llama-3.2-nv-rerankqa-1b-v2")


def test_probe_model_prefers_a_known_small_model():
    assert pick_probe_model([m["id"] for m in LISTING["data"]]) == "meta/llama-3.1-8b-instruct"
    assert pick_probe_model(["acme/foo-instruct"]) == "acme/foo-instruct"
    assert pick_probe_model(["nvidia/nv-embedqa-e5-v5"]) is None


async def test_adapter_list_models_skips_embedding_and_rerank(monkeypatch):
    install_mock_transport(monkeypatch, _nim())
    models = await NVIDIAProvider(api_key=KEY).list_models()
    ids = {m.model_id for m in models}
    assert ids == {
        "meta/llama-3.1-8b-instruct",
        "meta/llama-3.3-70b-instruct",
        "deepseek-ai/deepseek-r1",
    }
    assert {m.provider_id for m in models} == {"nvidia"}


async def test_discovered_models_use_the_routable_provider_id(monkeypatch):
    """Regression: models were tagged "nvidia_nim", which nothing could route."""
    from velune.providers.discovery.nvidia_nim import NVIDIANIMDiscovery
    from velune.providers.registry import ProviderRegistry

    monkeypatch.setenv("NVIDIA_API_KEY", KEY)
    install_mock_transport(monkeypatch, _nim())
    models = await NVIDIANIMDiscovery().discover()

    assert models and {m.provider_id for m in models} == {"nvidia"}
    assert not any("embed" in m.model_id for m in models)
    registry = ProviderRegistry()
    assert registry.get(models[0].provider_id) is not None
    assert registry.check_provider_available(models[0].provider_id)


async def test_discovery_failure_is_reported_not_silent(monkeypatch):
    from velune.providers.discovery.nvidia_nim import NVIDIANIMDiscovery

    monkeypatch.setenv("NVIDIA_API_KEY", KEY)
    install_mock_transport(monkeypatch, _nim(list_status=503))
    discoverer = NVIDIANIMDiscovery()
    assert await discoverer.discover() == []
    assert discoverer.last_error and "503" in discoverer.last_error


def test_legacy_nvidia_nim_ids_resolve_to_nvidia():
    from velune.providers.catalog import canonical_provider_id
    from velune.providers.registry import ProviderRegistry

    assert canonical_provider_id("nvidia_nim") == "nvidia"
    assert ProviderRegistry().get("nvidia_nim") is not None


# ---------------------------------------------------------------------------
# Inference error typing and stream robustness
# ---------------------------------------------------------------------------


def _req() -> InferenceRequest:
    return InferenceRequest(
        model_id="meta/llama-3.1-8b-instruct", messages=[{"role": "user", "content": "hi"}]
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderAuthenticationError),
        (403, ProviderPermissionError),
        (404, ModelNotFoundError),
        (500, InferenceError),
    ],
)
async def test_infer_and_stream_raise_typed_errors(monkeypatch, status, expected):
    install_mock_transport(monkeypatch, _nim(chat_status=status))
    with pytest.raises(expected):
        await NVIDIAProvider(api_key=KEY).infer(_req())
    with pytest.raises(expected):
        [c async for c in NVIDIAProvider(api_key=KEY).stream(_req())]


async def test_stream_tolerates_usage_only_and_null_content_chunks(monkeypatch):
    lines = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": None}}]},
        {"choices": [], "usage": {"total_tokens": 3}},
        {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    install_mock_transport(monkeypatch, handler)
    chunks = [c async for c in NVIDIAProvider(api_key=KEY).stream(_req())]
    assert "".join(c.content for c in chunks) == "Hello"


async def test_infer_handles_null_content(monkeypatch):
    body = {"choices": [{"message": {"content": None}, "finish_reason": "stop"}]}
    install_mock_transport(monkeypatch, _nim(chat_body=body))
    response = await NVIDIAProvider(api_key=KEY).infer(_req())
    assert response.content == ""
