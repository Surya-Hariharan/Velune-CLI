from __future__ import annotations

import asyncio
import logging

import httpx

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.providers.discovery.gpu import GPUDetector

logger = logging.getLogger("velune.providers.discovery.ollama")


class OllamaDiscovery:
    """Discovers models from Ollama."""

    def __init__(self):
        self.provider_id = "ollama"
        self.base_url = "http://localhost:11434"
        self.gpu_detector = GPUDetector()

    @classmethod
    async def is_running(cls) -> bool:
        """Return True if the Ollama daemon is reachable."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                r = await client.head("http://localhost:11434")
                return r.status_code < 500
        except Exception:
            return False

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

    async def discover(self) -> list[ModelDescriptor]:
        """Discover models from Ollama."""
        models = []

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                data = response.json()

                gpu_info = self.gpu_detector.detect()

                tasks = [self._get_model_details(model["name"]) for model in data.get("models", [])]
                details_list = await asyncio.gather(*tasks, return_exceptions=True)

                for model, details in zip(data.get("models", []), details_list):
                    if isinstance(details, dict) and "num_ctx" in details:
                        if "details" not in model or not isinstance(model["details"], dict):
                            model["details"] = {}
                        model["details"]["num_ctx"] = details["num_ctx"]

                    descriptor = self._parse_model(model, gpu_info)
                    if descriptor:
                        models.append(descriptor)
        except Exception as e:
            logger.debug("Ollama discovery failed: %s", e)

        return models

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
        return (param_count * vram_per_param) / 1e9

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

        return profile
