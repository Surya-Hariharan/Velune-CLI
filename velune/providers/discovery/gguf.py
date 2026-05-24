from __future__ import annotations
from pathlib import Path
from typing import List, Optional
from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel


class GGUFDiscovery:
    """Discovers GGUF models from filesystem."""

    def __init__(self):
        self.provider_id = "gguf"
        self.search_paths = [
            Path.home() / ".cache" / "huggingface" / "hub",
            Path.home() / "models",
            Path.cwd() / "models",
        ]

    async def discover(self) -> list[ModelDescriptor]:
        """Discover GGUF models from filesystem."""
        models = []
        
        for search_path in self.search_paths:
            if not search_path.exists():
                continue
            
            for gguf_file in search_path.rglob("*.gguf"):
                descriptor = self._parse_gguf_file(gguf_file)
                if descriptor:
                    models.append(descriptor)
        
        return models

    def _parse_gguf_file(self, gguf_path: Path) -> ModelDescriptor:
        """Parse GGUF file into descriptor."""
        try:
            from gguf import GGUFReader
            
            reader = GGUFReader(str(gguf_path))
            metadata = reader.metadata
            
            model_id = str(gguf_path.relative_to(Path.home()))
            display_name = gguf_path.stem
            
            # Extract metadata
            context_length = metadata.get("context_length", 4096)
            param_count = metadata.get("parameter_count", 0)
            
            # Convert parameter count to billions if huge (e.g. 7,000,000,000 instead of 7)
            if param_count > 1000:
                param_count_b = param_count / 1e9
            else:
                param_count_b = param_count
            
            # Classify capabilities
            capabilities = self._classify_capabilities(display_name)
            
            # Extract quantization
            quantization = self._extract_quantization(display_name)
            
            # Estimate VRAM required
            vram_required = self._estimate_vram(param_count_b, quantization)
            
            return ModelDescriptor(
                model_id=model_id,
                provider_id=self.provider_id,
                display_name=display_name,
                context_length=context_length,
                capabilities=capabilities,
                quantization=quantization,
                vram_required_gb=vram_required,
                parameter_count_b=param_count_b,
                speed_tier="medium",
                cost_per_1k_tokens=None,
                tags=["local", "gguf"],
                metadata={"gguf_metadata": metadata},
            )
        except Exception:
            return None

    def _estimate_vram(self, param_count_b: float, quantization: str) -> Optional[float]:
        """Estimate required VRAM in GB."""
        if not param_count_b:
            return None
            
        quant_lower = (quantization or "").lower()
        if "q4_k_m" in quant_lower or "q4" in quant_lower:
            bytes_per_param = 0.55
        elif "q8_0" in quant_lower or "q8" in quant_lower:
            bytes_per_param = 1.0
        elif "fp16" in quant_lower or "f16" in quant_lower:
            bytes_per_param = 2.0
        else:
            bytes_per_param = 0.55  # default fallback
            
        vram_gb = (param_count_b * bytes_per_param) + 0.5
        return vram_gb

    def _classify_capabilities(self, filename: str) -> ModelCapabilityProfile:
        """Classify capabilities from filename."""
        filename_lower = filename.lower()
        
        profile = ModelCapabilityProfile()
        
        if any(name in filename_lower for name in ["coder", "code"]):
            profile.coding = CapabilityLevel.CAPABLE
        else:
            profile.coding = CapabilityLevel.BASIC
        
        profile.reasoning = CapabilityLevel.BASIC
        profile.instruction_following = CapabilityLevel.BASIC
        
        return profile

    def _extract_quantization(self, filename: str) -> str:
        """Extract quantization from filename."""
        filename_lower = filename.lower()
        
        if "q4_k_m" in filename_lower:
            return "Q4_K_M"
        elif "q4_0" in filename_lower:
            return "Q4_0"
        elif "q4" in filename_lower:
            return "Q4"
        elif "q5_k_m" in filename_lower:
            return "Q5_K_M"
        elif "q5" in filename_lower:
            return "Q5"
        elif "q8_0" in filename_lower:
            return "Q8_0"
        elif "q8" in filename_lower:
            return "Q8"
        elif "f16" in filename_lower or "fp16" in filename_lower:
            return "FP16"
        
        return None
