"""Lightweight capability benchmarking."""

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile


class CapabilityBenchmark:
    """Lightweight benchmark probes for capability detection."""

    async def benchmark_coding(self, model_id: str) -> CapabilityLevel:
        """Benchmark coding capability."""
        # In production, this would run actual coding tasks
        # For now, return based on heuristics
        model_lower = model_id.lower()

        if any(name in model_lower for name in ["coder", "deepseek-coder"]):
            return CapabilityLevel.STRONG
        elif any(name in model_lower for name in ["llama", "mistral"]):
            return CapabilityLevel.CAPABLE

        return CapabilityLevel.BASIC

    async def benchmark_reasoning(self, model_id: str) -> CapabilityLevel:
        """Benchmark reasoning capability."""
        model_lower = model_id.lower()

        if any(name in model_lower for name in ["r1", "qwq", "deepseek-r1"]):
            return CapabilityLevel.EXCEPTIONAL
        elif any(name in model_lower for name in ["qwen"]):
            return CapabilityLevel.CAPABLE

        return CapabilityLevel.BASIC

    async def run_full_benchmark(self, model_id: str) -> ModelCapabilityProfile:
        """Run full capability benchmark."""
        profile = ModelCapabilityProfile()

        profile.coding = await self.benchmark_coding(model_id)
        profile.reasoning = await self.benchmark_reasoning(model_id)

        # Infer other capabilities
        if profile.reasoning >= CapabilityLevel.CAPABLE:
            profile.planning = CapabilityLevel.CAPABLE

        return profile
