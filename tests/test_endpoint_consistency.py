"""One configured base URL is used by validation, discovery and inference alike.

All traffic goes to an in-process mock transport; a request that strayed to the
vendor host would show up in the log below. No provider is contacted.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import httpx
import pytest

from tests.http_fakes import install_mock_transport, isolate_keystore
from velune.kernel.config import ProviderEntry, ProvidersConfig
from velune.providers import endpoints
from velune.providers import keystore as ks
from velune.providers.discovery.anthropic import AnthropicDiscovery
from velune.providers.discovery.openai import OpenAIDiscovery
from velune.providers.registry import ProviderRegistry
from velune.providers.validation import validate_provider

OPENAI_KEY = "sk-test-0000000000000000000000000"
ANTHROPIC_KEY = "sk-ant-test-000000000000000000000"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    isolate_keystore(monkeypatch, tmp_path)
    for pid in ("openai", "anthropic"):
        endpoints.configure_base_url(pid, None)
    with patch(
        "velune.providers.crypto.get_or_create_master_key",
        return_value=base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="),
    ):
        yield
    for pid in ("openai", "anthropic"):
        endpoints.configure_base_url(pid, None)


def _models(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {"id": "gpt-4o", "created": 1, "owned_by": "x"},
                {"id": "claude-sonnet-4-5", "type": "model", "display_name": "S"},
            ],
            "has_more": False,
        },
    )


async def test_openai_validation_discovery_and_the_adapter_share_the_configured_gateway(
    monkeypatch,
):
    seen = install_mock_transport(monkeypatch, _models)
    ks.save_key("openai", OPENAI_KEY, verified=True)

    registry = ProviderRegistry(
        ProvidersConfig(openai=ProviderEntry(base_url="https://gateway.example/v1/"))
    )
    adapter = registry.get("openai")
    assert adapter is not None

    await validate_provider("openai", OPENAI_KEY)
    await OpenAIDiscovery().discover()
    await adapter.health_check()

    assert seen, "nothing was requested"
    assert {r.url.host for r in seen} == {"gateway.example"}
    assert all(r.url.path.startswith("/v1/") for r in seen)
    assert not any(str(r.url).startswith("https://api.openai.com") for r in seen)


async def test_anthropic_validation_discovery_and_the_adapter_share_the_configured_gateway(
    monkeypatch,
):
    seen = install_mock_transport(monkeypatch, _models)
    ks.save_key("anthropic", ANTHROPIC_KEY, verified=True)

    # A base URL pasted from the docs with a trailing /v1 must not double it.
    registry = ProviderRegistry(
        ProvidersConfig(anthropic=ProviderEntry(base_url="https://claude-proxy.example/v1"))
    )
    adapter = registry.get("anthropic")
    assert adapter is not None

    await validate_provider("anthropic", ANTHROPIC_KEY)
    await AnthropicDiscovery().discover()
    await adapter.health_check()

    assert seen, "nothing was requested"
    assert {r.url.host for r in seen} == {"claude-proxy.example"}
    assert not any("/v1/v1" in r.url.path for r in seen)
