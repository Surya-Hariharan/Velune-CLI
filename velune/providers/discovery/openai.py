from __future__ import annotations
import os
import httpx
from typing import List
from velune.core.types.model import ModelDescriptor, ModelCapabilityProfile, CapabilityLevel


class OpenAIDiscovery:
    """Discovers models from OpenAI."""

    def __init__(self):
        self.provider_id = "openai"
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.base_url = "https://api.openai.com/v1"

    async def discover(self) -> list[ModelDescriptor]:
        """Discover models from OpenAI."""
        if not self.api_key:
            return []
        
        models = []
        
        try:
            headers = {"Authorization": f"Bearer {self.api_key}"}
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers=headers,
                )
                response.raise_for_status()
                data = response.json()
                
                for model in data.get("data", []):
                    if "gpt" in model["id"].lower():
                        descriptor = self._parse_model(model)
                        if descriptor:
                            models.append(descriptor)
        except Exception:
            pass
        
        return models

    def _parse_model(self, model_data: dict) -> ModelDescriptor:
        """Parse model data into descriptor."""
        model_id = model_data["id"]
        
        capabilities = self._classify_capabilities(model_id)
        
        # Determine context length and cost
        if "gpt-4" in model_id:
            context_length = 128000
            cost_per_1k = 0.03
        elif "gpt-3.5" in model_id:
            context_length = 16385
            cost_per_1k = 0.002
        else:
            context_length = 4096
            cost_per_1k = 0.001
        
        return ModelDescriptor(
            model_id=model_id,
            provider_id=self.provider_id,
            display_name=model_id,
            context_length=context_length,
            capabilities=capabilities,
            quantization=None,
            vram_required_gb=None,
            parameter_count_b=None,
            speed_tier="fast",
            cost_per_1k_tokens=cost_per_1k,
            tags=["cloud", "openai"],
            metadata={"raw": model_data},
        )

    def _classify_capabilities(self, model_id: str) -> ModelCapabilityProfile:
        """Classify capabilities for OpenAI models."""
        profile = ModelCapabilityProfile()
        
        if "gpt-4" in model_id:
            profile.coding = CapabilityLevel.STRONG
            profile.reasoning = CapabilityLevel.EXCEPTIONAL
            profile.planning = CapabilityLevel.EXCEPTIONAL
            profile.summarization = CapabilityLevel.STRONG
            profile.instruction_following = CapabilityLevel.EXCEPTIONAL
            profile.tool_use = CapabilityLevel.EXCEPTIONAL
            profile.long_context = CapabilityLevel.STRONG
        elif "gpt-3.5" in model_id:
            profile.coding = CapabilityLevel.CAPABLE
            profile.reasoning = CapabilityLevel.CAPABLE
            profile.planning = CapabilityLevel.CAPABLE
            profile.summarization = CapabilityLevel.CAPABLE
            profile.instruction_following = CapabilityLevel.CAPABLE
            profile.tool_use = CapabilityLevel.CAPABLE
        
        return profile
