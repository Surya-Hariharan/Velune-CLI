"""Layered Ollama model discovery.

Two sources, combined so the user always sees the truth:

1. **The HTTP API** (``/api/tags`` + ``/api/show``) when the daemon is running.
   This is *authoritative* — anything it returns is servable for inference, with
   accurate context lengths. The base URL honours ``OLLAMA_HOST`` rather than
   assuming ``localhost:11434``.

2. **The on-disk manifest store** (:mod:`velune.providers.ollama_store`) across
   every registered / ``OLLAMA_MODELS`` / default root. This finds models the
   daemon isn't serving — because it's stopped, or was started against a
   different ``OLLAMA_MODELS`` (e.g. models on an external drive). These are
   surfaced as *discovered but not currently servable*, with a clear reason,
   rather than hidden or silently failing.

Names always come from manifests / the API — never from opaque blob filenames.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.discovery.gpu import GPUDetector

logger = logging.getLogger("velune.providers.discovery.ollama")


def _base_url() -> str:
    """Resolve the Ollama API base URL, honouring ``OLLAMA_HOST``."""
    host = os.environ.get("OLLAMA_HOST", "").strip()
    if not host:
        return "http://localhost:11434"
    # OLLAMA_HOST may be "host:port", "http://host:port", or just a host.
    if host.startswith(("http://", "https://")):
        return host.rstrip("/")
    return f"http://{host}"


class OllamaDiscovery:
    """Discovers Ollama models from the daemon API and the on-disk stores."""

    def __init__(self):
        self.provider_id = "ollama"
        self.base_url = _base_url()
        self.gpu_detector = GPUDetector()

    @classmethod
    async def is_running(cls, base_url: str | None = None) -> bool:
        """Return True if the Ollama daemon is reachable."""
        url = (base_url or _base_url()).rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.head(url)
                return r.status_code < 500
        except Exception:
            return False

    async def discover(self) -> list[ModelDescriptor]:
        """Discover Ollama models, merging the API and the on-disk stores."""
        gpu_info = self.gpu_detector.detect()

        daemon_up = await self.is_running(self.base_url)
        api_models: list[ModelDescriptor] = []
        if daemon_up:
            api_models = await self._discover_api(gpu_info)

        api_names = {m.model_id for m in api_models}
        fs_models = self._discover_filesystem(api_names, daemon_up, gpu_info)
        return api_models + fs_models

    # ------------------------------------------------------------------
    # API source (authoritative — servable)
    # ------------------------------------------------------------------

    async def _discover_api(self, gpu_info: dict) -> list[ModelDescriptor]:
        models: list[ModelDescriptor] = []
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                data = response.json()

                tasks = [self._get_model_details(m["name"]) for m in data.get("models", [])]
                details_list = await asyncio.gather(*tasks, return_exceptions=True)

                for model, details in zip(data.get("models", []), details_list, strict=False):
                    if isinstance(details, dict) and "num_ctx" in details:
                        if not isinstance(model.get("details"), dict):
                            model["details"] = {}
                        model["details"]["num_ctx"] = details["num_ctx"]
                    descriptor = self._parse_model(model, gpu_info)
                    descriptor.metadata["servable"] = True
                    descriptor.metadata["source"] = "api"
                    models.append(descriptor)
        except Exception as e:
            logger.debug("Ollama API discovery failed: %s", e)
        return models

    async def _get_model_details(self, model_name: str) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(f"{self.base_url}/api/show", json={"name": model_name})
                data = r.json()
                modelfile = data.get("modelfile", "")
                for line in modelfile.splitlines():
                    if line.strip().upper().startswith("PARAMETER NUM_CTX"):
                        try:
                            return {"num_ctx": int(line.split()[-1])}
                        except ValueError:
                            pass
        except Exception:
            pass
        return {}

    # ------------------------------------------------------------------
    # Filesystem source (manifest store — may be unservable)
    # ------------------------------------------------------------------

    def _discover_filesystem(
        self, api_names: set[str], daemon_up: bool, gpu_info: dict
    ) -> list[ModelDescriptor]:
        """Find on-disk models the API didn't return, flagged for servability."""
        from velune.providers.ollama_locations import OllamaLocationRegistry

        try:
            stores = OllamaLocationRegistry().active_stores()
        except Exception as exc:
            logger.debug("Filesystem model discovery skipped: %s", exc)
            return []

        out: list[ModelDescriptor] = []
        seen: set[str] = set(api_names)
        for store in stores:
            for stored in store.list_models():
                if stored.name in seen:
                    continue  # already served by the daemon (authoritative)
                seen.add(stored.name)
                out.append(self._descriptor_from_store(stored, daemon_up, gpu_info))
        return out

    def _descriptor_from_store(self, stored, daemon_up: bool, gpu_info: dict) -> ModelDescriptor:
        from velune.providers.ollama_store import OllamaStoredModel

        assert isinstance(stored, OllamaStoredModel)
        quant = stored.quantization or self._extract_quantization(stored.name)
        vram = self._estimate_vram_from_params(stored.parameter_count_b, quant)

        if daemon_up:
            reason = (
                "Discovered on disk but the running Ollama daemon was not started "
                "with this OLLAMA_MODELS root, so it cannot be served. Point the "
                "daemon at this root (set OLLAMA_MODELS and restart `ollama serve`) "
                "to use it."
            )
        else:
            reason = (
                "Ollama daemon is not running - start it with `ollama serve` to use this model."
            )

        return ModelDescriptor(
            model_id=stored.name,
            provider_id=self.provider_id,
            display_name=stored.name,
            context_length=stored.context_length or 8192,
            capabilities=self._classify_capabilities(stored.name),
            is_local=True,
            quantization=quant,
            vram_required_gb=vram,
            parameter_count_b=stored.parameter_count_b,
            speed_tier="medium",
            cost_per_1k_tokens=None,
            location=str(stored.root),
            health="offline",
            tags=["local", "ollama", "filesystem", "unservable"],
            metadata={
                "servable": False,
                "servable_reason": reason,
                "source": "manifest",
                "root": str(stored.root),
                "size_bytes": stored.size_bytes,
                "family": stored.family,
            },
        )

    # ------------------------------------------------------------------
    # Shared parsing helpers
    # ------------------------------------------------------------------

    def _parse_model(self, model_data: dict, gpu_info: dict) -> ModelDescriptor:
        model_id = model_data["name"]
        details = model_data.get("details", {})
        quantization = self._extract_quantization(model_id)
        vram_gb = self._estimate_vram(details, quantization)
        capabilities = self._classify_capabilities(model_id)

        return ModelDescriptor(
            model_id=model_id,
            provider_id=self.provider_id,
            display_name=model_id,
            context_length=details.get("num_ctx", 4096),
            capabilities=capabilities,
            is_local=True,
            quantization=quantization,
            vram_required_gb=vram_gb,
            parameter_count_b=details.get("parameter_count"),
            speed_tier="medium",
            cost_per_1k_tokens=None,
            location=self.base_url,
            health="unknown",
            tags=["local", "ollama"],
            metadata={"details": details},
        )

    def _extract_quantization(self, model_id: str) -> str | None:
        quant_map = {
            "q4_k_m": "Q4_K_M",
            "q4_0": "Q4_0",
            "q5_k_m": "Q5_K_M",
            "q5_0": "Q5_0",
            "q8_0": "Q8_0",
            "fp16": "FP16",
            "f16": "F16",
        }
        model_lower = model_id.lower()
        for key, value in quant_map.items():
            if key in model_lower:
                return value
        return None

    def _estimate_vram(self, details: dict, quantization: str | None) -> float | None:
        param_count = details.get("parameter_count", 0)
        if not param_count:
            return None
        return self._estimate_vram_from_params(param_count / 1e9, quantization)

    def _estimate_vram_from_params(
        self, param_count_b: float | None, quantization: str | None
    ) -> float | None:
        if not param_count_b:
            return None
        vram_per_param_gb = {
            None: 2.0,
            "FP16": 2.0,
            "F16": 2.0,
            "Q8_0": 1.0,
            "Q5_K_M": 0.7,
            "Q5_0": 0.7,
            "Q4_K_M": 0.5,
            "Q4_0": 0.5,
        }
        vram_per_param = vram_per_param_gb.get(quantization, 0.5)
        return round(param_count_b * vram_per_param, 1)

    def _classify_capabilities(self, model_id: str) -> ModelCapabilityProfile:
        model_lower = model_id.lower()
        profile = ModelCapabilityProfile()

        if any(name in model_lower for name in ["coder", "code", "deepseek-coder", "qwen-coder"]):
            profile.coding = CapabilityLevel.ADVANCED
        elif any(name in model_lower for name in ["llama", "mistral", "qwen"]):
            profile.coding = CapabilityLevel.INTERMEDIATE

        if any(name in model_lower for name in ["r1", "reason", "deepseek-r1"]):
            profile.reasoning = CapabilityLevel.ADVANCED
        elif any(name in model_lower for name in ["qwq", "qwen"]):
            profile.reasoning = CapabilityLevel.INTERMEDIATE

        if profile.reasoning >= CapabilityLevel.INTERMEDIATE:
            profile.planning = CapabilityLevel.INTERMEDIATE

        if any(name in model_lower for name in ["llama", "mistral"]):
            profile.summarization = CapabilityLevel.INTERMEDIATE

        if "instruct" in model_lower or "chat" in model_lower:
            profile.instruction_following = CapabilityLevel.INTERMEDIATE

        if any(name in model_lower for name in ["long", "32k", "128k"]):
            profile.long_context = CapabilityLevel.INTERMEDIATE

        if profile.instruction_following >= CapabilityLevel.INTERMEDIATE:
            profile.tool_use = CapabilityLevel.INTERMEDIATE

        if any(
            name in model_lower
            for name in ["llava", "vision", "moondream", "vl", "minicpm-v", "bakllava"]
        ):
            profile.vision = CapabilityLevel.ADVANCED
            profile.multimodal = CapabilityLevel.ADVANCED

        if any(name in model_lower for name in ["embed", "bge-", "e5-", "gte-", "nomic-embed"]):
            profile.embedding = CapabilityLevel.EXPERT

        return profile
