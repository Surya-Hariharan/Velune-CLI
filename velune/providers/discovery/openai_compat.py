"""Discovery for generic OpenAI-compatible servers running on localhost.

Covers self-hosted inference servers that expose the OpenAI ``/v1/models`` and
``/v1/chat/completions`` API on the common local ports — vLLM, llama.cpp's
``server``, LocalAI, text-generation-webui, etc. — without requiring the user to
know any filesystem paths (Rule 6/7). Ollama (:11434) and LM Studio (:1234) have
their own discoverers and are intentionally skipped here to avoid double-listing.
"""

from __future__ import annotations

import logging

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor

logger = logging.getLogger("velune.providers.discovery.openai_compat")

# Common ports for self-hosted OpenAI-compatible servers (vLLM, LocalAI, …).
# Deliberately excludes 1234 (LM Studio) and 11434 (Ollama).
_CANDIDATE_PORTS: tuple[int, ...] = (8000, 8080, 3000)


def _base_urls() -> list[str]:
    return [f"http://localhost:{port}/v1" for port in _CANDIDATE_PORTS]


class OpenAICompatDiscovery:
    """Discovers models from generic OpenAI-compatible local servers."""

    def __init__(self) -> None:
        self.provider_id = "openai-compat"

    @classmethod
    async def is_running(cls) -> bool:
        """Return True if any candidate OpenAI-compatible server answers."""
        for base_url in _base_urls():
            try:
                async with httpx.AsyncClient(timeout=1.5) as client:
                    r = await client.get(f"{base_url}/models")
                    if r.status_code < 500:
                        return True
            except Exception:
                continue
        return False

    async def discover(self) -> list[ModelDescriptor]:
        """Query each reachable local endpoint's ``/v1/models``.

        Models are de-duplicated by ``model_id`` (first endpoint wins). Each
        descriptor records its endpoint in ``metadata["base_url"]`` so the
        matching adapter can target the right port.
        """
        seen: set[str] = set()
        models: list[ModelDescriptor] = []
        for base_url in _base_urls():
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(f"{base_url}/models")
                    response.raise_for_status()
                    data = response.json()
            except Exception as e:
                logger.debug("OpenAI-compatible discovery failed for %s: %s", base_url, e)
                continue

            for item in data.get("data", []):
                descriptor = self._parse_model(item, base_url)
                if descriptor and descriptor.model_id not in seen:
                    seen.add(descriptor.model_id)
                    models.append(descriptor)
        return models

    def _parse_model(self, model_data: dict, base_url: str) -> ModelDescriptor | None:
        model_id = model_data.get("id")
        if not model_id:
            return None
        return ModelDescriptor(
            model_id=model_id,
            provider_id=self.provider_id,
            display_name=model_id,
            context_length=8192,
            capabilities=self._classify_capabilities(model_id),
            is_local=True,
            quantization=None,
            vram_required_gb=None,
            parameter_count_b=None,
            speed_tier="medium",
            cost_per_1k_tokens=None,
            tags=["local", "openai-compat"],
            metadata={"base_url": base_url, "raw": model_data},
        )

    def _classify_capabilities(self, model_id: str) -> ModelCapabilityProfile:
        model_lower = model_id.lower()
        profile = ModelCapabilityProfile()
        if any(name in model_lower for name in ["coder", "code"]):
            profile.coding = CapabilityLevel.INTERMEDIATE
        else:
            profile.coding = CapabilityLevel.BASIC
        profile.reasoning = CapabilityLevel.BASIC
        profile.instruction_following = CapabilityLevel.INTERMEDIATE
        profile.summarization = CapabilityLevel.BASIC
        return profile
