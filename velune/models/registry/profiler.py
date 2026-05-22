"""Model profiling (latency, quality, cost)."""

import time
from typing import Dict, Optional
from dataclasses import dataclass
from velune.core.types import InferenceRequest, InferenceResponse


@dataclass
class ModelProfile:
    """Profile data for a model."""
    model_id: str
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    sample_count: int
    last_updated: float


class ModelProfiler:
    """Profiler for model performance metrics."""

    def __init__(self):
        self._profiles: Dict[str, ModelProfile] = {}
        self._latency_samples: Dict[str, list[float]] = {}

    def record_inference(
        self, model_id: str, latency_ms: float
    ) -> None:
        """Record an inference latency sample."""
        if model_id not in self._latency_samples:
            self._latency_samples[model_id] = []
        
        self._latency_samples[model_id].append(latency_ms)
        
        # Update profile
        samples = self._latency_samples[model_id]
        sorted_samples = sorted(samples)
        
        self._profiles[model_id] = ModelProfile(
            model_id=model_id,
            avg_latency_ms=sum(samples) / len(samples),
            p50_latency_ms=sorted_samples[len(sorted_samples) // 2],
            p95_latency_ms=sorted_samples[int(len(sorted_samples) * 0.95)],
            p99_latency_ms=sorted_samples[int(len(sorted_samples) * 0.99)],
            sample_count=len(samples),
            last_updated=time.time(),
        )

    def get_profile(self, model_id: str) -> Optional[ModelProfile]:
        """Get the profile for a model."""
        return self._profiles.get(model_id)

    def get_latency_estimate(self, model_id: str) -> Optional[float]:
        """Get estimated latency for a model."""
        profile = self.get_profile(model_id)
        return profile.p95_latency_ms if profile else None

    def list_profiles(self) -> list[ModelProfile]:
        """List all model profiles."""
        return list(self._profiles.values())
