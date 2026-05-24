"""Unit tests for the empirical capability probing and caching subsystem."""

from __future__ import annotations

import json
from pathlib import Path
import time
import pytest

from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel
from velune.core.types.inference import InferenceRequest, InferenceResponse
from velune.providers.base import ModelProvider
from velune.models.probes import ModelProber, ProbeResult
from velune.models.profile_cache import ModelProfileCache
from velune.models.registry import ModelCapabilityRegistry


class MockProvider(ModelProvider):
    def __init__(self, responses: dict[str, str]) -> None:
        self.responses = responses
        self.inferences: list[InferenceRequest] = []

    @property
    def provider_id(self) -> str:
        return "mock"

    async def list_models(self) -> list:
        return []

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        self.inferences.append(request)
        prompt = request.messages[0]["content"]

        content = "Default mock answer"
        for key, resp in self.responses.items():
            if key in prompt:
                content = resp
                break

        return InferenceResponse(
            content=content,
            model_id=request.model_id,
            finish_reason="stop",
            tokens_used=50,
            latency_ms=10.0,
        )

    async def stream(self, request: InferenceRequest):
        raise NotImplementedError()

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        return []

    async def health_check(self) -> Any:
        return None

    def get_capabilities(self) -> Any:
        return None

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


@pytest.mark.asyncio
async def test_model_prober_successful():
    responses = {
        "Sieve of Eratosthenes": "def sieve(n):\n    primes = [True] * (n + 1)\n    for i in range(2, int(n**0.5) + 1):\n        if primes[i]:\n            for j in range(i*i, n + 1, i): primes[j] = False\n    return [x for x in range(2, n + 1) if primes[x]]",
        "bloops are razzles": "Yes. If A subset B and B subset C, then A subset C.",
        "JSON object": '{"status": "ok", "count": 42}',
    }
    provider = MockProvider(responses)
    prober = ModelProber(provider, "test-model")

    results = await prober.run_all_probes()

    assert results["coding"].score == 1.0
    assert results["coding"].passed is True
    assert results["coding"].latency_ms > 0

    assert results["reasoning"].score == 1.0
    assert results["reasoning"].passed is True

    assert results["instruction"].score == 1.0
    assert results["instruction"].passed is True


@pytest.mark.asyncio
async def test_model_prober_failures():
    provider = MockProvider({})
    prober = ModelProber(provider, "test-model")

    results = await prober.run_all_probes()

    assert results["coding"].score == 0.0
    assert results["coding"].passed is False

    assert results["reasoning"].score == 0.0
    assert results["reasoning"].passed is False

    assert results["instruction"].score == 0.0
    assert results["instruction"].passed is False


def test_model_profile_cache(tmp_path):
    cache_path = tmp_path / "profiles.json"
    cache = ModelProfileCache(cache_path)

    probe_results = {
        "coding": ProbeResult("coding", 0.8, 150.0, True, "details"),
        "reasoning": ProbeResult("reasoning", 0.9, 100.0, True, "details"),
        "instruction": ProbeResult("instruction", 0.5, 50.0, True, "details"),
    }

    # Store in cache
    cache.set("model-a", "prov-a", probe_results)

    # Retrieve from cache
    cached = cache.get("model-a", "prov-a")
    assert cached is not None
    assert cached["probes"]["coding"]["score"] == 0.8
    assert cached["probes"]["reasoning"]["passed"] is True

    # TTL Expiration check
    entry = json.loads(cache_path.read_text())
    entry["prov-a/model-a"]["probed_at"] = time.time() - (169 * 3600)  # Over 168 hours ago
    cache_path.write_text(json.dumps(entry))

    assert cache.get("model-a", "prov-a") is None


@pytest.mark.asyncio
async def test_registry_integration(tmp_path, monkeypatch):
    mock_cache_file = tmp_path / "model_profiles.json"

    class MockScanner:
        async def scan_all(self):
            return [
                ModelDescriptor(
                    id="emp-model",
                    provider="mock-prov",
                    name="Empirical Model",
                    context_window=2048,
                    is_local=True,
                    capabilities=ModelCapabilityProfile(),
                    speed_tier="fast"
                )
            ]

    registry = ModelCapabilityRegistry(scanner=MockScanner())

    # Prep cache with dummy empirical data
    cache = ModelProfileCache(mock_cache_file)
    cache.set("emp-model", "mock-prov", {
        "coding": ProbeResult("coding", 0.85, 120.0, True),
        "reasoning": ProbeResult("reasoning", 0.95, 95.0, True),
        "instruction": ProbeResult("instruction", 0.45, 60.0, True),
    })

    # Redirect cache file inside ModelCapabilityRegistry.refresh()
    def mock_init(self, cache_path):
        self.cache_path = mock_cache_file
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(ModelProfileCache, "__init__", mock_init)

    await registry.refresh()

    model = registry.get("emp-model", "mock-prov")
    assert model is not None
    assert model.capabilities.coding == CapabilityLevel.ADVANCED
    assert model.capabilities.reasoning == CapabilityLevel.EXPERT
    assert model.capabilities.instruction_following == CapabilityLevel.INTERMEDIATE
    assert model.capabilities.planning == CapabilityLevel.INTERMEDIATE
