from __future__ import annotations

import logging
from pathlib import Path

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor

logger = logging.getLogger("velune.providers.discovery.gguf")


class GGUFDiscovery:
    """Discovers GGUF models from filesystem using LocalModelResolver."""

    def __init__(self):
        self.provider_id = "gguf"

    async def discover(self) -> list[ModelDescriptor]:
        """Discover GGUF models across all well-known paths."""
        from velune.providers.local_resolver import LocalModelResolver

        resolver = LocalModelResolver()
        files = resolver.scan_gguf_files()

        models: list[ModelDescriptor] = []
        for gguf_path in files:
            descriptor = self._build_descriptor(gguf_path, resolver)
            if descriptor is not None:
                models.append(descriptor)
        return models

    def _build_descriptor(self, gguf_path: Path, resolver) -> ModelDescriptor | None:
        try:
            meta = resolver.get_model_metadata(gguf_path)

            try:
                model_id = str(gguf_path.relative_to(Path.home()))
            except ValueError:
                model_id = str(gguf_path)

            display_name = gguf_path.stem
            param_count_b = meta.get("param_count_b")
            quantization = meta.get("quantization")
            context_length = meta.get("context_length") or 4096
            capabilities = self._classify_capabilities(display_name)
            vram_gb = self._estimate_vram(param_count_b, quantization)

            return ModelDescriptor(
                model_id=model_id,
                provider_id=self.provider_id,
                display_name=display_name,
                context_length=context_length,
                capabilities=capabilities,
                quantization=quantization,
                vram_required_gb=vram_gb,
                parameter_count_b=param_count_b,
                speed_tier="medium",
                cost_per_1k_tokens=None,
                location=str(gguf_path),
                health="unknown",
                tags=["local", "gguf"],
                metadata={"gguf_path": str(gguf_path), "family": meta.get("family")},
            )
        except Exception:
            logger.debug("Failed to build descriptor for %s", gguf_path, exc_info=True)
            return None

    def _estimate_vram(self, param_count_b: float | None, quantization: str | None) -> float | None:
        if not param_count_b:
            return None
        quant_lower = (quantization or "").lower()
        if "q4" in quant_lower:
            bpp = 0.55
        elif "q8" in quant_lower:
            bpp = 1.0
        elif "fp16" in quant_lower or "f16" in quant_lower:
            bpp = 2.0
        else:
            bpp = 0.55
        return param_count_b * bpp + 0.5

    def _classify_capabilities(self, filename: str) -> ModelCapabilityProfile:
        lower = filename.lower()
        profile = ModelCapabilityProfile()
        if any(kw in lower for kw in ("coder", "code")):
            profile.coding = CapabilityLevel.INTERMEDIATE
        else:
            profile.coding = CapabilityLevel.BASIC
        profile.reasoning = CapabilityLevel.BASIC
        profile.instruction_following = CapabilityLevel.BASIC
        return profile
