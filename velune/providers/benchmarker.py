"""Provider and model benchmarking engine."""

from __future__ import annotations

import time

from velune.core.types.inference import InferenceRequest
from velune.providers.base import ModelProvider


class ModelBenchmarkMetrics:
    """Live performance metrics recorded during model benchmarks."""

    def __init__(self) -> None:
        self.ttft_ms: float = 0.0  # Time to first token
        self.tps: float = 0.0  # Tokens per second
        self.total_latency_ms: float = 0.0  # Total latency in ms
        self.tokens_generated: int = 0
        self.tool_accuracy: float = 0.0  # Percentage correctness for tools
        self.json_validity: float = 0.0  # Percentage valid JSON formats


class ProviderBenchmarker:
    """Evaluates provider latency, generation throughput, and structured accuracy."""

    def __init__(self, provider: ModelProvider, model_id: str) -> None:
        self.provider = provider
        self.model_id = model_id

    async def run_latency_probe(
        self, prompt: str = "Hello, respond with exactly 'pong'."
    ) -> ModelBenchmarkMetrics:
        """Probe time-to-first-token and total generation latency."""
        metrics = ModelBenchmarkMetrics()
        request = InferenceRequest(
            model_id=self.model_id,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=10,
        )

        start_time = time.perf_counter()
        first_token_time: float | None = None
        chars_received = 0

        try:
            # We use streaming to calculate TTFT and TPS
            async for chunk in self.provider.stream(request):
                if first_token_time is None and chunk.content:
                    first_token_time = time.perf_counter()
                    metrics.ttft_ms = (first_token_time - start_time) * 1000.0

                chars_received += len(chunk.content)
                # Estimating tokens: 1 token ≈ 4 characters
                metrics.tokens_generated = int(chars_received / 4.0)

            end_time = time.perf_counter()
            metrics.total_latency_ms = (end_time - start_time) * 1000.0

            if first_token_time is not None:
                gen_duration = end_time - first_token_time
                if gen_duration > 0 and metrics.tokens_generated > 0:
                    metrics.tps = metrics.tokens_generated / gen_duration
            else:
                metrics.ttft_ms = metrics.total_latency_ms
                metrics.tps = 0.0

        except Exception:
            # Handle non-streaming fallback
            try:
                start_time = time.perf_counter()
                response = await self.provider.infer(request)
                end_time = time.perf_counter()

                metrics.total_latency_ms = (end_time - start_time) * 1000.0
                metrics.ttft_ms = metrics.total_latency_ms  # No stream TTFT estimation
                metrics.tokens_generated = response.tokens_used or int(len(response.content) / 4.0)

                duration = end_time - start_time
                if duration > 0:
                    metrics.tps = metrics.tokens_generated / duration
            except Exception:
                # Failed probe
                metrics.ttft_ms = -1.0
                metrics.tps = 0.0

        return metrics

    async def run_structured_probe(self) -> dict[str, float]:
        """Probe model capabilities in generating valid JSON format."""
        prompt = (
            "Return a JSON object representing a file list with keys 'files' (list of strings) and 'count' (integer). "
            "Return ONLY raw valid JSON code. No markdown wrapper, no extra text."
        )
        request = InferenceRequest(
            model_id=self.model_id,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=100,
        )

        json_valid = 0.0
        structure_valid = 0.0

        try:
            response = await self.provider.infer(request)
            content = response.content.strip()

            # Clean possible markdown wrappers
            if content.startswith("```"):
                lines = content.splitlines()
                if len(lines) > 2:
                    content = "\n".join(lines[1:-1])
                    if content.startswith("json"):
                        content = content[4:].strip()

            import json

            parsed = json.loads(content)
            json_valid = 1.0

            if isinstance(parsed, dict) and "files" in parsed and "count" in parsed:
                if isinstance(parsed["files"], list) and isinstance(parsed["count"], int):
                    structure_valid = 1.0
        except Exception:
            pass

        return {"json_validity": json_valid, "structure_validity": structure_valid}

    async def evaluate(self) -> ModelBenchmarkMetrics:
        """Run all benchmark probes and aggregate metrics."""
        metrics = await self.run_latency_probe()
        struct_res = await self.run_structured_probe()

        metrics.json_validity = struct_res["json_validity"]
        metrics.tool_accuracy = struct_res["structure_validity"]

        return metrics
