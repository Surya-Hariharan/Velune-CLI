"""Capability classification engine."""

from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor


class CapabilityClassifier:
    """Classifies model capabilities using multiple strategies."""

    def __init__(self):
        self.name_patterns = {
            "coding": ["coder", "code", "deepseek-coder", "qwen-coder", "starcoder"],
            "reasoning": ["r1", "reason", "deepseek-r1", "qwq", "qwen"],
            "planning": ["qwq", "qwen", "r1"],
            "summarization": ["llama", "mistral"],
            "instruction_following": ["instruct", "chat"],
            "long_context": ["long", "32k", "128k", "200k"],
        }

    def classify(self, model: ModelDescriptor) -> ModelCapabilityProfile:
        """Classify capabilities for a model."""
        profile = ModelCapabilityProfile()
        model_lower = model.model_id.lower()

        # Name-based classification
        for capability, patterns in self.name_patterns.items():
            if any(pattern in model_lower for pattern in patterns):
                setattr(profile, capability, self._infer_level(model_lower, capability))

        # Architecture-based inference
        if "moe" in model_lower or "mixture-of-experts" in model_lower:
            profile.reasoning = max(profile.reasoning, CapabilityLevel.INTERMEDIATE)

        # GGUF metadata parsing
        if model.metadata.get("gguf_metadata"):
            self._parse_gguf_metadata(model.metadata["gguf_metadata"], profile)

        return profile

    def _infer_level(self, model_id: str, capability: str) -> CapabilityLevel:
        """Infer capability level from model name."""
        model_lower = model_id.lower()

        # Strong indicators
        if any(indicator in model_lower for indicator in ["v2", "latest", "pro"]):
            return CapabilityLevel.ADVANCED

        # Capable indicators
        if any(indicator in model_lower for indicator in ["coder", "instruct"]):
            return CapabilityLevel.INTERMEDIATE

        # Basic indicators
        if capability == "reasoning" and "r1" in model_lower:
            return CapabilityLevel.EXPERT

        return CapabilityLevel.BASIC

    def _parse_gguf_metadata(self, metadata: dict, profile: ModelCapabilityProfile) -> None:
        """Parse GGUF metadata for capability hints."""
        # Check for long context
        context_length = metadata.get("context_length", 0)
        if context_length >= 32000:
            profile.long_context = CapabilityLevel.INTERMEDIATE
        elif context_length >= 8000:
            profile.long_context = CapabilityLevel.BASIC

        # Check parameter count for capability inference
        param_count = metadata.get("parameter_count", 0)
        if param_count >= 70e9:  # 70B+
            profile.reasoning = max(profile.reasoning, CapabilityLevel.INTERMEDIATE)
