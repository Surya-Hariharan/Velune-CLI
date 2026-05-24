from __future__ import annotations
import httpx
from typing import List
from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel
from velune.providers.discovery.gpu import GPUDetector


class OllamaDiscovery:
    """Discovers models from Ollama."""

    def __init__(self):
        self.provider_id = "ollama"
        self.base_url = "http://localhost:11434"
        self.gpu_detector = GPUDetector()

    async def discover(self) -> list[ModelDescriptor]:
        """Discover models from Ollama."""
        models = []
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                data = response.json()
                
                gpu_info = self.gpu_detector.detect()
                
                for model in data.get("models", []):
                    descriptor = self._parse_model(model, gpu_info)
                    if descriptor:
                        models.append(descriptor)
        except Exception:
            pass
        
        return models

    def _parse_model(self, model_data: dict, gpu_info: dict) -> ModelDescriptor:
        """Parse model data into descriptor."""
        model_id = model_data["name"]
        details = model_data.get("details", {})
        
        # Extract quantization from model name
        quantization = self._extract_quantization(model_id)
        
        # Estimate VRAM based on quantization and parameter count
        vram_gb = self._estimate_vram(details, quantization)
        
        # Classify capabilities based on model name
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

    def _extract_quantization(self, model_id: str) -> str:
        """Extract quantization from model ID."""
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

    def _estimate_vram(self, details: dict, quantization: str) -> float:
        """Estimate VRAM requirements."""
        param_count = details.get("parameter_count", 0)
        if param_count == 0:
            return None
        
        # Rough VRAM estimation based on quantization
        vram_per_param_gb = {
            None: 2.0,      # FP16
            "FP16": 2.0,
            "F16": 2.0,
            "Q8_0": 1.0,
            "Q5_K_M": 0.7,
            "Q5_0": 0.7,
            "Q4_K_M": 0.5,
            "Q4_0": 0.5,
        }
        
        vram_per_param = vram_per_param_gb.get(quantization, 0.5)
        return (param_count * vram_per_param) / 1e9  # Convert to GB

    def _classify_capabilities(self, model_id: str) -> ModelCapabilityProfile:
        """Classify model capabilities based on name."""
        model_lower = model_id.lower()
        
        profile = ModelCapabilityProfile()
        
        # Coding capability
        if any(name in model_lower for name in ["coder", "code", "deepseek-coder", "qwen-coder"]):
            profile.coding = CapabilityLevel.STRONG
        elif any(name in model_lower for name in ["llama", "mistral", "qwen"]):
            profile.coding = CapabilityLevel.CAPABLE
        
        # Reasoning capability
        if any(name in model_lower for name in ["r1", "reason", "deepseek-r1"]):
            profile.reasoning = CapabilityLevel.STRONG
        elif any(name in model_lower for name in ["qwq", "qwen"]):
            profile.reasoning = CapabilityLevel.CAPABLE
        
        # Planning capability
        if profile.reasoning >= CapabilityLevel.CAPABLE:
            profile.planning = CapabilityLevel.CAPABLE
        
        # Summarization
        if any(name in model_lower for name in ["llama", "mistral"]):
            profile.summarization = CapabilityLevel.CAPABLE
        
        # Instruction following
        if "instruct" in model_lower or "chat" in model_lower:
            profile.instruction_following = CapabilityLevel.CAPABLE
        
        # Long context
        if any(name in model_lower for name in ["long", "32k", "128k"]):
            profile.long_context = CapabilityLevel.CAPABLE
        
        # Tool use
        if profile.instruction_following >= CapabilityLevel.CAPABLE:
            profile.tool_use = CapabilityLevel.CAPABLE
        
        return profile
