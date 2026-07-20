"""Model profiling and dynamic metrics analysis."""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.providers.base import ModelProvider
from velune.providers.benchmarker import ModelBenchmarkMetrics, ProviderBenchmarker


class ModelProfile(BaseModel):
    """Profile representing capability, speed, and real-time execution statistics."""

    model_id: str
    provider_id: str
    avg_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    sample_count: int = 0
    tps: float = 0.0  # Tokens per second
    ttft_ms: float = 0.0  # Time to first token
    json_validity: float = 1.0  # Percentage structured compliance
    last_updated: float = Field(default_factory=time.time)


class ModelProfiler:
    """Measures accuracy, response speed, and structure-matching capabilities."""

    def __init__(self) -> None:
        self._profiles: dict[str, ModelProfile] = {}
        self._latency_samples: dict[str, list[float]] = {}

    def record_execution(self, provider_id: str, model_id: str, latency_ms: float) -> None:
        """Record real-time execution latency to build statistical latency profiles."""
        key = f"{provider_id}/{model_id}"
        if key not in self._latency_samples:
            self._latency_samples[key] = []

        samples = self._latency_samples[key]
        samples.append(latency_ms)

        # Enforce rolling history bounds to prevent memory bloat
        if len(samples) > 100:
            samples.pop(0)

        sorted_samples = sorted(samples)
        n = len(sorted_samples)

        avg_lat = sum(samples) / n
        p95_lat = sorted_samples[int(n * 0.95)] if n > 0 else avg_lat

        if key in self._profiles:
            profile = self._profiles[key]
            profile.avg_latency_ms = avg_lat
            profile.p95_latency_ms = p95_lat
            profile.sample_count = n
            profile.last_updated = time.time()
        else:
            self._profiles[key] = ModelProfile(
                model_id=model_id,
                provider_id=provider_id,
                avg_latency_ms=avg_lat,
                p95_latency_ms=p95_lat,
                sample_count=n,
            )

    async def profile_model(
        self, provider: ModelProvider, descriptor: ModelDescriptor
    ) -> ModelProfile:
        """Actively benchmark an operational provider model for performance and structure."""
        key = f"{descriptor.provider_id}/{descriptor.model_id}"

        # Run benchmarks
        benchmarker = ProviderBenchmarker(provider, descriptor.model_id)
        metrics: ModelBenchmarkMetrics = await benchmarker.evaluate()

        # Build or update the profile
        profile = self._profiles.get(key)
        if not profile:
            profile = ModelProfile(
                model_id=descriptor.model_id,
                provider_id=descriptor.provider_id,
            )
            self._profiles[key] = profile

        profile.tps = metrics.tps
        profile.ttft_ms = metrics.ttft_ms
        profile.json_validity = metrics.json_validity
        profile.last_updated = time.time()

        # Update capability profile levels based on empirical benchmark results
        capabilities = getattr(descriptor, "capabilities", None)
        if capabilities and hasattr(capabilities, "tool_use") and metrics.json_validity < 0.5:
            # Degrade tool use if model repeatedly fails structure test.
            #
            # The demotion is also recorded on the descriptor because
            # ModelCapabilityProfile.tool_use *defaults* to NONE: most discovery
            # paths never set it, so a bare NONE says "unknown", not "incapable".
            # Only this flag marks a NONE that is backed by measurement, and it
            # is what the tool-loop gate keys off — otherwise honouring NONE
            # would disable tools for every model with an unpopulated profile.
            capabilities.tool_use = CapabilityLevel.NONE
            try:
                descriptor.metadata["tool_use_demoted"] = True
            except Exception:  # pragma: no cover - descriptor without metadata
                pass

        return profile

    def get_profile(self, provider_id: str, model_id: str) -> ModelProfile | None:
        """Look up the recorded profile for a model."""
        key = f"{provider_id}/{model_id}"
        return self._profiles.get(key)

    def list_profiles(self) -> list[ModelProfile]:
        """Enumerate all active profiles."""
        return list(self._profiles.values())
