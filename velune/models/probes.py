"""Empirical capability probes and model prober engine."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ProbeResult:
    capability: str
    score: float  # 0.0 to 1.0
    latency_ms: float
    passed: bool
    details: str = ""


CODING_PROBE = """Write a Python function that finds all prime numbers up to n using the Sieve of Eratosthenes. Return only the function, no explanation."""

REASONING_PROBE = """If all bloops are razzles, and all razzles are lazzles, are all bloops lazzles? Answer with just Yes or No, then one sentence explanation."""

INSTRUCTION_PROBE = """Respond with ONLY the JSON object {"status": "ok", "count": 42}. Nothing else."""


def _score_coding_response(response: str) -> float:
    """Score coding probe response (0.0 to 1.0)."""
    score = 0.0
    if "def " in response:
        score += 0.3
    if "range(" in response:
        score += 0.2
    if "sieve" in response.lower() or ("for" in response and "%" in response):
        score += 0.3
    if "return" in response:
        score += 0.2
    return min(1.0, score)


def _score_reasoning_response(response: str) -> float:
    """Score reasoning probe response (0.0 to 1.0)."""
    cleaned = response.strip().lower()
    if not cleaned:
        return 0.0

    score = 0.0
    if "yes" in cleaned:
        score += 0.7
        if len(cleaned) > 10:
            score += 0.3
    return min(1.0, score)


def _score_instruction_response(response: str) -> float:
    """Score instruction following probe response (0.0 to 1.0)."""
    cleaned = response.strip()
    # Clean possible markdown block wrappers
    for prefix in ("```json", "```"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        import json
        data = json.loads(cleaned)
        if isinstance(data, dict):
            if data.get("status") == "ok" and data.get("count") == 42:
                return 1.0
            return 0.5
    except Exception:
        pass
    return 0.0


class ModelProber:
    """Runs lightweight capability probes against a specific model via its provider."""

    def __init__(self, provider: Any, model_id: str) -> None:
        self.provider = provider
        self.model_id = model_id

    async def run_coding_probe(self) -> ProbeResult:
        """Run the coding capability probe."""
        from velune.core.types.inference import InferenceRequest
        start = time.perf_counter()
        if not self.provider:
            return ProbeResult("coding", 0.0, -1.0, False, "Provider not available")

        try:
            req = InferenceRequest(
                model_id=self.model_id,
                messages=[{"role": "user", "content": CODING_PROBE}],
                temperature=0.1,
                max_tokens=200,
            )
            response = await self.provider.infer(req)
            latency_ms = (time.perf_counter() - start) * 1000.0
            score = _score_coding_response(response.content)
            return ProbeResult("coding", score, latency_ms, score > 0.5, response.content[:100])
        except Exception as e:
            return ProbeResult("coding", 0.0, -1.0, False, str(e))

    async def run_reasoning_probe(self) -> ProbeResult:
        """Run the deductive reasoning capability probe."""
        from velune.core.types.inference import InferenceRequest
        start = time.perf_counter()
        if not self.provider:
            return ProbeResult("reasoning", 0.0, -1.0, False, "Provider not available")

        try:
            req = InferenceRequest(
                model_id=self.model_id,
                messages=[{"role": "user", "content": REASONING_PROBE}],
                temperature=0.1,
                max_tokens=100,
            )
            response = await self.provider.infer(req)
            latency_ms = (time.perf_counter() - start) * 1000.0
            score = _score_reasoning_response(response.content)
            return ProbeResult("reasoning", score, latency_ms, score > 0.5, response.content[:100])
        except Exception as e:
            return ProbeResult("reasoning", 0.0, -1.0, False, str(e))

    async def run_instruction_probe(self) -> ProbeResult:
        """Run the strict JSON instruction following capability probe."""
        from velune.core.types.inference import InferenceRequest
        start = time.perf_counter()
        if not self.provider:
            return ProbeResult("instruction", 0.0, -1.0, False, "Provider not available")

        try:
            req = InferenceRequest(
                model_id=self.model_id,
                messages=[{"role": "user", "content": INSTRUCTION_PROBE}],
                temperature=0.1,
                max_tokens=100,
            )
            response = await self.provider.infer(req)
            latency_ms = (time.perf_counter() - start) * 1000.0
            score = _score_instruction_response(response.content)
            return ProbeResult("instruction", score, latency_ms, score > 0.5, response.content[:100])
        except Exception as e:
            return ProbeResult("instruction", 0.0, -1.0, False, str(e))

    async def run_all_probes(self) -> dict[str, ProbeResult]:
        """Run all capability probes in parallel."""
        import asyncio
        coding, reasoning, instruction = await asyncio.gather(
            self.run_coding_probe(),
            self.run_reasoning_probe(),
            self.run_instruction_probe(),
        )
        return {"coding": coding, "reasoning": reasoning, "instruction": instruction}


class FastProbe:
    """Single lightweight probe to validate a model is responding."""

    PING_PROMPT = "Reply with exactly the word: PONG"
    TIMEOUT = 10.0

    async def ping(self, provider: Any, model_id: str) -> bool:
        """Returns True if model responds within timeout."""
        import asyncio

        from velune.core.types.inference import InferenceRequest
        try:
            req = InferenceRequest(
                model_id=model_id,
                messages=[{"role": "user", "content": self.PING_PROMPT}],
                temperature=0.0,
                max_tokens=5,
            )
            response = await asyncio.wait_for(
                provider.infer(req),
                timeout=self.TIMEOUT,
            )
            return bool(response.content.strip())
        except Exception:
            return False
