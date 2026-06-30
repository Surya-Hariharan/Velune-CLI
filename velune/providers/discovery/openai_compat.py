"""Discovery for generic OpenAI-compatible servers running on localhost.

Covers self-hosted inference servers that expose the OpenAI ``/v1/models`` and
``/v1/chat/completions`` API — vLLM, llama.cpp's ``server``, LocalAI,
text-generation-webui, TGI (OpenAI-compat mode), and others — without requiring
the user to know any filesystem paths.

Ollama (:11434) and LM Studio (:1234) have their own discoverers and are
intentionally excluded from the candidate port list to avoid double-listing.

Server fingerprinting tries to identify the specific server type so models are
tagged with ``vllm``, ``tgi``, ``localai``, etc., and assigned a matching
``provider_id``.

Custom endpoints can be added via the ``VELUNE_EXTRA_ENDPOINTS`` environment
variable as a comma-separated list of base URLs (e.g.
``http://192.168.1.10:8000/v1,http://my-rig:9000``).
"""

from __future__ import annotations

import logging
import os

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor

logger = logging.getLogger("velune.providers.discovery.openai_compat")

# Common ports for self-hosted OpenAI-compatible servers.
# Deliberately excludes 1234 (LM Studio) and 11434 (Ollama).
_CANDIDATE_PORTS: tuple[int, ...] = (8000, 8080, 8888, 3000, 5000, 5001, 7860, 9000, 4000)

# Probe paths to fingerprint the server type (checked in order; first match wins).
# Each entry: (path, expected_response_key_or_None, server_type, provider_id)
_FINGERPRINT_PROBES: tuple[tuple[str, str | None, str, str], ...] = (
    ("/health", None, "vllm", "vllm"),
    ("/info", "model_id", "tgi", "tgi"),
    ("/readyz", None, "localai", "localai"),
)


def _candidate_base_urls() -> list[str]:
    standard = [f"http://localhost:{port}/v1" for port in _CANDIDATE_PORTS]
    extra_raw = os.environ.get("VELUNE_EXTRA_ENDPOINTS", "").strip()
    if extra_raw:
        for raw_url in extra_raw.split(","):
            raw_url = raw_url.strip()
            if raw_url:
                # Normalise: ensure the URL ends with /v1
                if not raw_url.endswith("/v1"):
                    raw_url = raw_url.rstrip("/") + "/v1"
                standard.append(raw_url)
    return standard


async def _fingerprint(client: httpx.AsyncClient, base_url: str) -> tuple[str, str]:
    """Return (server_type, provider_id) by probing well-known paths.

    Falls back to ("openai-compat", "openai-compat") if nothing matches.
    """
    server_base = base_url.removesuffix("/v1").rstrip("/")
    for path, expected_key, server_type, provider_id in _FINGERPRINT_PROBES:
        try:
            r = await client.get(f"{server_base}{path}", timeout=1.5)
            if r.status_code < 500:
                if expected_key is None:
                    return server_type, provider_id
                try:
                    if expected_key in r.json():
                        return server_type, provider_id
                except Exception:
                    return server_type, provider_id
        except Exception:
            continue
    return "openai-compat", "openai-compat"


class OpenAICompatDiscovery:
    """Discovers models from generic OpenAI-compatible local servers."""

    def __init__(self) -> None:
        self.provider_id = "openai-compat"

    @classmethod
    async def is_running(cls) -> bool:
        """Return True if any candidate OpenAI-compatible server answers."""
        for base_url in _candidate_base_urls():
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

        Models are de-duplicated by ``(model_id, location)`` pair.  Each
        descriptor records its endpoint in ``location`` and ``metadata["base_url"]``.
        """
        seen: set[tuple[str, str]] = set()
        models: list[ModelDescriptor] = []

        for base_url in _candidate_base_urls():
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    response = await client.get(f"{base_url}/models")
                    response.raise_for_status()
                    data = response.json()
                    server_type, provider_id = await _fingerprint(client, base_url)
            except Exception as e:
                logger.debug("OpenAI-compatible discovery failed for %s: %s", base_url, e)
                continue

            for item in data.get("data", []):
                descriptor = self._parse_model(item, base_url, server_type, provider_id)
                if descriptor is None:
                    continue
                key = (descriptor.model_id, base_url)
                if key in seen:
                    continue
                seen.add(key)
                models.append(descriptor)

        return models

    def _parse_model(
        self, model_data: dict, base_url: str, server_type: str, provider_id: str
    ) -> ModelDescriptor | None:
        model_id = model_data.get("id")
        if not model_id:
            return None

        capabilities = self._classify_capabilities(model_id)
        tags = ["local", server_type]
        if capabilities.vision > CapabilityLevel.NONE:
            tags.append("vision")
        if capabilities.embedding > CapabilityLevel.NONE:
            tags.append("embedding")

        return ModelDescriptor(
            model_id=model_id,
            provider_id=provider_id,
            display_name=model_id,
            context_length=8192,
            capabilities=capabilities,
            is_local=True,
            location=base_url,
            health="unknown",
            tags=tags,
            metadata={"base_url": base_url, "server_type": server_type, "raw": model_data},
        )

    def _classify_capabilities(self, model_id: str) -> ModelCapabilityProfile:
        model_lower = model_id.lower()
        profile = ModelCapabilityProfile()

        if any(name in model_lower for name in ["coder", "code", "starcoder"]):
            profile.coding = CapabilityLevel.INTERMEDIATE
        else:
            profile.coding = CapabilityLevel.BASIC

        if any(name in model_lower for name in ["r1", "reason", "qwq"]):
            profile.reasoning = CapabilityLevel.ADVANCED
        else:
            profile.reasoning = CapabilityLevel.BASIC

        if any(name in model_lower for name in ["embed", "bge-", "e5-", "gte-"]):
            profile.embedding = CapabilityLevel.ADVANCED

        if any(name in model_lower for name in ["vision", "llava", "vl", "moondream", "minicpm-v"]):
            profile.vision = CapabilityLevel.ADVANCED
            profile.multimodal = CapabilityLevel.ADVANCED

        profile.instruction_following = CapabilityLevel.INTERMEDIATE
        profile.summarization = CapabilityLevel.BASIC

        if any(name in model_lower for name in ["instruct", "chat"]):
            profile.tool_use = CapabilityLevel.INTERMEDIATE

        return profile
