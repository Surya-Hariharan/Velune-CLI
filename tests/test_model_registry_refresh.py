"""Refreshing the model catalog never loses models because a listing failed.

Covers the scan report, the merge rules, discovery failure reporting for
OpenAI/Anthropic, and the shared Ollama endpoint resolver.
"""

from __future__ import annotations

import base64
from unittest.mock import patch

import httpx
import pytest

from tests.http_fakes import install_mock_transport, isolate_keystore
from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.models.local_models import LocalModelRegistry
from velune.models.registry import ModelCapabilityRegistry, is_available
from velune.providers import endpoints
from velune.providers import keystore as ks
from velune.providers.discovery.scanner import ScanReport


def _m(mid: str, pid: str, *, local: bool = False, **meta) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=mid,
        provider_id=pid,
        display_name=mid,
        context_length=8192,
        capabilities={"coding": CapabilityLevel.ADVANCED},  # a dict, like curated catalogs
        is_local=local,
        metadata=dict(meta),
    )


class _Scanner:
    """A scanner whose next report the test controls."""

    def __init__(self) -> None:
        self.report = ScanReport()
        self.raise_next: Exception | None = None

    async def scan_all_detailed(self) -> ScanReport:
        if self.raise_next:
            raise self.raise_next
        return self.report


@pytest.fixture
def registry(tmp_path, monkeypatch):
    isolate_keystore(monkeypatch, tmp_path)
    from velune.models import registry_cache

    monkeypatch.setattr(registry_cache, "DEFAULT_CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.chdir(tmp_path)  # profile cache is cwd-relative
    scanner = _Scanner()
    reg = ModelCapabilityRegistry(scanner, local_models=LocalModelRegistry(tmp_path / "local.json"))
    reg.scanner_double = scanner
    return reg


async def _refresh(reg, *, models=(), refreshed=(), failed=None, skipped=()):
    reg.scanner_double.report = ScanReport(
        models=list(models),
        refreshed=set(refreshed),
        failed=dict(failed or {}),
        skipped=set(skipped),
    )
    await reg.refresh()


def _ids(reg) -> set[tuple[str, str]]:
    return {(m.provider_id, m.model_id) for m in reg.list_all()}


async def test_failed_listing_keeps_the_last_known_models(registry):
    await _refresh(
        registry, models=[_m("gpt-4o", "openai"), _m("o3", "openai")], refreshed={"openai"}
    )
    await _refresh(registry, failed={"openai": "OpenAI model listing failed (HTTP 403)"})

    assert {("openai", "gpt-4o"), ("openai", "o3")} <= _ids(registry)
    kept = registry.get("o3", "openai")
    # A failed cloud listing says nothing about whether the model works.
    assert is_available(kept)
    assert "403" in kept.metadata["listing_stale"]


async def test_curated_fallback_does_not_replace_a_known_list(registry):
    await _refresh(registry, models=[_m("gpt-5", "openai")], refreshed={"openai"})
    fallback = _m("gpt-4o", "openai", catalog_source="fallback")
    await _refresh(registry, models=[fallback], failed={"openai": "timed out"})
    assert _ids(registry) == {("openai", "gpt-5")}


async def test_a_successful_listing_does_remove_models_the_provider_dropped(registry):
    await _refresh(
        registry, models=[_m("old", "openai"), _m("new", "openai")], refreshed={"openai"}
    )
    await _refresh(registry, models=[_m("new", "openai")], refreshed={"openai"})
    assert _ids(registry) == {("openai", "new")}


async def test_scan_exception_leaves_the_catalog_untouched(registry):
    await _refresh(registry, models=[_m("gpt-4o", "openai")], refreshed={"openai"})
    registry.scanner_double.raise_next = RuntimeError("boom")
    await registry.refresh()
    assert _ids(registry) == {("openai", "gpt-4o")}


async def test_other_providers_are_unaffected_by_one_failing(registry):
    await _refresh(
        registry,
        models=[_m("gpt-4o", "openai"), _m("claude-x", "anthropic")],
        refreshed={"openai", "anthropic"},
    )
    await _refresh(
        registry,
        models=[_m("claude-y", "anthropic")],
        refreshed={"anthropic"},
        failed={"openai": "offline"},
    )
    assert _ids(registry) == {("openai", "gpt-4o"), ("anthropic", "claude-y")}


async def test_local_models_are_marked_unavailable_not_deleted(registry):
    await _refresh(registry, models=[_m("qwen:7b", "ollama", local=True)], refreshed={"ollama"})
    await _refresh(registry, failed={"ollama": "Ollama server is not reachable at http://x:1"})
    kept = registry.get("qwen:7b", "ollama")
    assert kept is not None and not is_available(kept)
    assert kept.metadata["unavailable_kind"] == "endpoint_unreachable"


async def test_removed_key_drops_that_providers_models(registry):
    await _refresh(registry, models=[_m("gpt-4o", "openai")], refreshed={"openai"})
    await _refresh(registry, skipped={"openai"})
    assert _ids(registry) == set()


async def test_rejected_key_keeps_models_but_says_why(registry):
    with patch(
        "velune.providers.crypto.get_or_create_master_key",
        return_value=base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="),
    ):
        ks.save_key("openai", "sk-test-0000000000000000000000000")
        ks.mark_invalid("openai", reason="401")
        await _refresh(registry, models=[_m("gpt-4o", "openai")], refreshed={"openai"})
        await _refresh(registry, skipped={"openai"})
    assert ("openai", "gpt-4o") in _ids(registry)


async def test_dict_capabilities_from_curated_catalogs_do_not_break_refresh(registry):
    await _refresh(registry, models=[_m("gpt-4o", "openai")], refreshed={"openai"})
    model = registry.get("gpt-4o", "openai")
    registry._apply_probe_results(model, {"coding": {"score": 0.9}, "reasoning": {"score": 0.6}})
    assert model.capabilities.coding is CapabilityLevel.EXPERT


async def test_refreshed_catalog_survives_restart(registry, tmp_path):
    await _refresh(registry, models=[_m("gpt-4o", "openai")], refreshed={"openai"})
    again = ModelCapabilityRegistry(
        _Scanner(), local_models=LocalModelRegistry(tmp_path / "local.json")
    )
    assert ("openai", "gpt-4o") in {(m.provider_id, m.model_id) for m in again.list_all()}


# ---------------------------------------------------------------------------
# Discoverers report failures and read the key at scan time
# ---------------------------------------------------------------------------


@pytest.fixture
def keystore_with_master(tmp_path, monkeypatch):
    isolate_keystore(monkeypatch, tmp_path)
    endpoints.configure_base_url("openai", None)
    endpoints.configure_base_url("anthropic", None)
    with patch(
        "velune.providers.crypto.get_or_create_master_key",
        return_value=base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="),
    ):
        yield


async def test_openai_discoverer_reads_a_key_added_after_construction(
    monkeypatch, keystore_with_master
):
    from velune.providers.discovery.openai import OpenAIDiscovery

    discoverer = OpenAIDiscovery()  # built before any key exists, like the cached scanner's
    ks.save_key("openai", "sk-test-0000000000000000000000000")
    install_mock_transport(
        monkeypatch,
        lambda r: httpx.Response(
            200, json={"data": [{"id": "gpt-5"}, {"id": "ft:gpt-4o-mini:acme::x1"}]}
        ),
    )
    models = await discoverer.discover()
    ids = {m.model_id for m in models}
    assert {"gpt-5", "ft:gpt-4o-mini:acme::x1"} <= ids
    assert discoverer.last_error is None


async def test_openai_listing_failure_is_reported_and_fallback_is_flagged(
    monkeypatch, keystore_with_master
):
    from velune.providers.discovery.openai import OpenAIDiscovery

    ks.save_key("openai", "sk-test-0000000000000000000000000")
    install_mock_transport(
        monkeypatch, lambda r: httpx.Response(403, json={"error": {"message": "x"}})
    )
    discoverer = OpenAIDiscovery()
    models = await discoverer.discover()
    assert discoverer.last_error and "403" in discoverer.last_error
    assert models and all(m.metadata["catalog_source"] == "fallback" for m in models)


async def test_anthropic_listing_follows_pagination(monkeypatch, keystore_with_master):
    from velune.providers.discovery.anthropic import AnthropicDiscovery

    ks.save_key("anthropic", "sk-ant-test-000000000000000000000")

    def handler(request: httpx.Request) -> httpx.Response:
        if "after_id" in request.url.params:
            return httpx.Response(
                200, json={"data": [{"id": "claude-page-two"}], "has_more": False}
            )
        return httpx.Response(
            200,
            json={
                "data": [{"id": "claude-page-one"}],
                "has_more": True,
                "last_id": "claude-page-one",
            },
        )

    install_mock_transport(monkeypatch, handler)
    ids = {m.model_id for m in await AnthropicDiscovery().discover()}
    assert {"claude-page-one", "claude-page-two"} <= ids


def test_responses_only_and_non_chat_openai_models_are_not_listed():
    from velune.providers.adapters.openai import is_openai_chat_model

    assert is_openai_chat_model("gpt-4o")
    assert is_openai_chat_model("ft:gpt-4o-mini:org::abc")
    assert not is_openai_chat_model("gpt-5-pro")
    assert not is_openai_chat_model("o3-deep-research")
    assert not is_openai_chat_model("text-embedding-3-large")


def test_anthropic_requests_do_not_send_default_top_p():
    from velune.core.types.inference import InferenceRequest
    from velune.providers.adapters.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key="sk-ant-x")
    req = InferenceRequest(
        model_id="claude-sonnet-4-5", messages=[{"role": "user", "content": "hi"}]
    )
    assert "top_p" not in provider._build_payload(req)
    narrowed = req.model_copy(update={"top_p": 0.5})
    assert provider._build_payload(narrowed)["top_p"] == 0.5


# ---------------------------------------------------------------------------
# One Ollama endpoint for everyone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "url"),
    [
        ("", "http://localhost:11434"),
        ("myhost", "http://myhost:11434"),
        ("myhost:11500", "http://myhost:11500"),
        (":11500", "http://127.0.0.1:11500"),
        ("0.0.0.0", "http://127.0.0.1:11434"),
        ("https://ollama.example", "https://ollama.example:443"),
        ("http://10.0.0.5:11434/", "http://10.0.0.5:11434"),
    ],
)
def test_ollama_host_forms_normalize(value, url):
    assert endpoints.normalize_ollama_host(value) == url


def test_every_ollama_caller_uses_the_same_endpoint(monkeypatch):
    from velune.providers.adapters.ollama import OllamaProvider
    from velune.providers.discovery.ollama import OllamaDiscovery
    from velune.providers.ollama_manager import _ollama_base
    from velune.providers.registry import ProviderRegistry

    endpoints.configure_ollama_url(None)
    monkeypatch.setenv("OLLAMA_HOST", "gpu-box:11500")
    expected = "http://gpu-box:11500"

    assert OllamaProvider()._base_url == expected
    assert OllamaDiscovery().base_url == expected
    assert _ollama_base() == expected
    adapter = ProviderRegistry().get("ollama")
    assert adapter._inner._base_url == expected  # unwrap RetryingProvider
    assert endpoints.ollama_socket_address() == ("gpu-box", 11500)


def test_explicit_config_beats_ollama_host(monkeypatch):
    from velune.kernel.config import ProviderEntry, ProvidersConfig
    from velune.providers.registry import ProviderRegistry

    monkeypatch.setenv("OLLAMA_HOST", "gpu-box:11500")
    try:
        ProviderRegistry(
            ProvidersConfig(ollama=ProviderEntry(base_url="http://192.168.1.20:11434"))
        )
        assert endpoints.ollama_base_url() == "http://192.168.1.20:11434"
    finally:
        endpoints.configure_ollama_url(None)


async def test_ollama_validation_uses_ollama_host(monkeypatch):
    from velune.providers.validation import validate_provider

    endpoints.configure_ollama_url(None)
    monkeypatch.setenv("OLLAMA_HOST", "gpu-box:11500")
    seen = install_mock_transport(
        monkeypatch, lambda r: httpx.Response(200, json={"models": [{"name": "qwen:7b"}]})
    )
    result = await validate_provider("ollama")
    assert result.ok and result.models == ["qwen:7b"]
    assert seen[0].url.host == "gpu-box" and seen[0].url.port == 11500
