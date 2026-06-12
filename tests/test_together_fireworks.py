"""Tests for Together.AI and Fireworks.AI provider adapters."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from velune.providers.adapters.fireworks import FireworksProvider
from velune.providers.adapters.together import TogetherProvider
from velune.providers.discovery.fireworks import FireworksDiscovery
from velune.providers.discovery.together import TogetherDiscovery

# ── TogetherProvider unit tests ───────────────────────────────────────────────


def test_together_provider_id():
    p = TogetherProvider(api_key="test-key")
    assert p.provider_id == "together"


def test_together_base_url_default():
    p = TogetherProvider(api_key="test-key")
    assert p._base_url == "https://api.together.xyz/v1"


def test_together_base_url_override():
    p = TogetherProvider(api_key="test-key", base_url="https://custom.url/v1")
    assert p._base_url == "https://custom.url/v1"


def test_together_api_key_stored():
    p = TogetherProvider(api_key="my-together-key")
    assert p._api_key == "my-together-key"


@pytest.mark.asyncio
async def test_together_list_models_count():
    p = TogetherProvider(api_key="test-key")
    models = await p.list_models()
    assert len(models) == 5


@pytest.mark.asyncio
async def test_together_list_models_provider_ids():
    p = TogetherProvider(api_key="test-key")
    models = await p.list_models()
    assert all(m.provider_id == "together" for m in models)


@pytest.mark.asyncio
async def test_together_has_llama_model():
    p = TogetherProvider(api_key="test-key")
    models = await p.list_models()
    ids = [m.model_id for m in models]
    assert any("Llama-3.3-70B" in mid for mid in ids)


@pytest.mark.asyncio
async def test_together_has_deepseek_model():
    p = TogetherProvider(api_key="test-key")
    models = await p.list_models()
    ids = [m.model_id for m in models]
    assert any("DeepSeek" in mid for mid in ids)


@pytest.mark.asyncio
async def test_together_has_qwen_coder():
    p = TogetherProvider(api_key="test-key")
    models = await p.list_models()
    ids = [m.model_id for m in models]
    assert any("Qwen2.5-Coder" in mid for mid in ids)


@pytest.mark.asyncio
async def test_together_health_check_unhealthy_no_client():
    p = TogetherProvider(api_key=None)
    health = await p.health_check()
    from velune.core.types.provider import ProviderHealth

    assert health == ProviderHealth.UNHEALTHY


# ── FireworksProvider unit tests ──────────────────────────────────────────────


def test_fireworks_provider_id():
    p = FireworksProvider(api_key="test-key")
    assert p.provider_id == "fireworks"


def test_fireworks_base_url_default():
    p = FireworksProvider(api_key="test-key")
    assert p._base_url == "https://api.fireworks.ai/inference/v1"


def test_fireworks_api_key_stored():
    p = FireworksProvider(api_key="my-fireworks-key")
    assert p._api_key == "my-fireworks-key"


@pytest.mark.asyncio
async def test_fireworks_list_models_count():
    p = FireworksProvider(api_key="test-key")
    models = await p.list_models()
    assert len(models) == 4


@pytest.mark.asyncio
async def test_fireworks_list_models_provider_ids():
    p = FireworksProvider(api_key="test-key")
    models = await p.list_models()
    assert all(m.provider_id == "fireworks" for m in models)


@pytest.mark.asyncio
async def test_fireworks_has_deepseek():
    p = FireworksProvider(api_key="test-key")
    models = await p.list_models()
    ids = [m.model_id for m in models]
    assert any("deepseek-r1" in mid for mid in ids)


@pytest.mark.asyncio
async def test_fireworks_has_mixtral():
    p = FireworksProvider(api_key="test-key")
    models = await p.list_models()
    ids = [m.model_id for m in models]
    assert any("mixtral" in mid for mid in ids)


@pytest.mark.asyncio
async def test_fireworks_model_ids_use_accounts_prefix():
    p = FireworksProvider(api_key="test-key")
    models = await p.list_models()
    assert all(m.model_id.startswith("accounts/fireworks/") for m in models)


@pytest.mark.asyncio
async def test_fireworks_health_check_unhealthy_no_client():
    p = FireworksProvider(api_key=None)
    health = await p.health_check()
    from velune.core.types.provider import ProviderHealth

    assert health == ProviderHealth.UNHEALTHY


# ── TogetherDiscovery tests ────────────────────────────────────────────────────


def test_together_discovery_provider_id():
    d = TogetherDiscovery()
    assert d.provider_id == "together"


@pytest.mark.asyncio
async def test_together_discovery_returns_empty_without_key():
    d = TogetherDiscovery()
    with patch("velune.providers.discovery.together.get_key", return_value=None):
        result = await d.discover()
    assert result == []


@pytest.mark.asyncio
async def test_together_discovery_returns_models_with_key():
    d = TogetherDiscovery()
    with patch("velune.providers.discovery.together.get_key", return_value="test-key"):
        result = await d.discover()
    assert len(result) == 5
    assert all(m.provider_id == "together" for m in result)


@pytest.mark.asyncio
async def test_together_discovery_speed_tiers_valid():
    d = TogetherDiscovery()
    with patch("velune.providers.discovery.together.get_key", return_value="test-key"):
        models = await d.discover()
    valid_tiers = {"fast", "medium", "slow"}
    for m in models:
        assert m.speed_tier in valid_tiers, f"{m.model_id} has invalid speed_tier: {m.speed_tier}"


# ── FireworksDiscovery tests ──────────────────────────────────────────────────


def test_fireworks_discovery_provider_id():
    d = FireworksDiscovery()
    assert d.provider_id == "fireworks"


@pytest.mark.asyncio
async def test_fireworks_discovery_returns_empty_without_key():
    d = FireworksDiscovery()
    with patch("velune.providers.discovery.fireworks.get_key", return_value=None):
        result = await d.discover()
    assert result == []


@pytest.mark.asyncio
async def test_fireworks_discovery_returns_models_with_key():
    d = FireworksDiscovery()
    with patch("velune.providers.discovery.fireworks.get_key", return_value="test-key"):
        result = await d.discover()
    assert len(result) == 4
    assert all(m.provider_id == "fireworks" for m in result)


@pytest.mark.asyncio
async def test_fireworks_discovery_speed_tiers_valid():
    d = FireworksDiscovery()
    with patch("velune.providers.discovery.fireworks.get_key", return_value="test-key"):
        models = await d.discover()
    valid_tiers = {"fast", "medium", "slow"}
    for m in models:
        assert m.speed_tier in valid_tiers, f"{m.model_id} has invalid speed_tier: {m.speed_tier}"


# ── Token tracker coverage ────────────────────────────────────────────────────


def test_together_costs_registered():
    from velune.telemetry.token_tracker import PROVIDER_COSTS

    assert "together" in PROVIDER_COSTS
    assert len(PROVIDER_COSTS["together"]) == 5


def test_fireworks_costs_registered():
    from velune.telemetry.token_tracker import PROVIDER_COSTS

    assert "fireworks" in PROVIDER_COSTS
    assert len(PROVIDER_COSTS["fireworks"]) == 4


def test_together_llama_cost():
    from velune.telemetry.token_tracker import PROVIDER_COSTS

    cost = PROVIDER_COSTS["together"]["meta-llama/Llama-3.3-70B-Instruct-Turbo"]
    assert cost > 0


def test_fireworks_deepseek_cost():
    from velune.telemetry.token_tracker import PROVIDER_COSTS

    cost = PROVIDER_COSTS["fireworks"]["accounts/fireworks/models/deepseek-r1"]
    assert cost > 0


# ── Setup wizard coverage ─────────────────────────────────────────────────────


def test_together_in_provider_metadata():
    from velune.cli.commands.setup import PROVIDER_METADATA

    assert "together" in PROVIDER_METADATA
    assert PROVIDER_METADATA["together"]["requires_key"] is True


def test_fireworks_in_provider_metadata():
    from velune.cli.commands.setup import PROVIDER_METADATA

    assert "fireworks" in PROVIDER_METADATA
    assert PROVIDER_METADATA["fireworks"]["requires_key"] is True


# ── Keystore env-var coverage ─────────────────────────────────────────────────


def test_together_env_var_registered():
    from velune.providers.keystore import _ENV_VARS

    assert "together" in _ENV_VARS
    assert _ENV_VARS["together"] == "TOGETHER_API_KEY"


def test_fireworks_env_var_registered():
    from velune.providers.keystore import _ENV_VARS

    assert "fireworks" in _ENV_VARS
    assert _ENV_VARS["fireworks"] == "FIREWORKS_API_KEY"


# ── Registry coverage ─────────────────────────────────────────────────────────


def test_together_in_registry():
    from velune.providers.registry import ProviderRegistry

    reg = ProviderRegistry()
    assert "together" in reg.list_providers()


def test_fireworks_in_registry():
    from velune.providers.registry import ProviderRegistry

    reg = ProviderRegistry()
    assert "fireworks" in reg.list_providers()
