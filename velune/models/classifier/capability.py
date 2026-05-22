"""Capability classification logic."""

from typing import Dict, Optional
from velune.core.types import ModelDescriptor, ModelCapability, CapabilityLevel


class CapabilityClassifier:
    """Classifies model capabilities."""

    def classify_model(
        self, model_id: str, provider: str
    ) -> Dict[ModelCapability, CapabilityLevel]:
        """Classify capabilities for a model."""
        # This is a simplified classifier
        # In production, this would use benchmarking results
        
        capabilities: Dict[ModelCapability, CapabilityLevel] = {}
        
        if provider == "openai":
            if "gpt-4" in model_id:
                capabilities = {
                    ModelCapability.CODE_GENERATION: CapabilityLevel.ADVANCED,
                    ModelCapability.CODE_ANALYSIS: CapabilityLevel.ADVANCED,
                    ModelCapability.REASONING: CapabilityLevel.EXPERT,
                    ModelCapability.PLANNING: CapabilityLevel.EXPERT,
                    ModelCapability.RETRIEVAL: CapabilityLevel.ADVANCED,
                    ModelCapability.SUMMARIZATION: CapabilityLevel.ADVANCED,
                    ModelCapability.DEBUGGING: CapabilityLevel.ADVANCED,
                    ModelCapability.REFACTORING: CapabilityLevel.ADVANCED,
                    ModelCapability.MULTILINGUAL: CapabilityLevel.EXPERT,
                    ModelCapability.TOOL_USE: CapabilityLevel.EXPERT,
                }
            elif "gpt-3.5" in model_id:
                capabilities = {
                    ModelCapability.CODE_GENERATION: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.CODE_ANALYSIS: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.REASONING: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.PLANNING: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.RETRIEVAL: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.SUMMARIZATION: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.DEBUGGING: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.REFACTORING: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.MULTILINGUAL: CapabilityLevel.ADVANCED,
                    ModelCapability.TOOL_USE: CapabilityLevel.ADVANCED,
                }
        
        elif provider == "anthropic":
            if "opus" in model_id:
                capabilities = {
                    ModelCapability.CODE_GENERATION: CapabilityLevel.ADVANCED,
                    ModelCapability.CODE_ANALYSIS: CapabilityLevel.ADVANCED,
                    ModelCapability.REASONING: CapabilityLevel.EXPERT,
                    ModelCapability.PLANNING: CapabilityLevel.EXPERT,
                    ModelCapability.RETRIEVAL: CapabilityLevel.ADVANCED,
                    ModelCapability.SUMMARIZATION: CapabilityLevel.EXPERT,
                    ModelCapability.DEBUGGING: CapabilityLevel.ADVANCED,
                    ModelCapability.REFACTORING: CapabilityLevel.ADVANCED,
                    ModelCapability.MULTILINGUAL: CapabilityLevel.EXPERT,
                    ModelCapability.TOOL_USE: CapabilityLevel.EXPERT,
                }
            elif "sonnet" in model_id:
                capabilities = {
                    ModelCapability.CODE_GENERATION: CapabilityLevel.ADVANCED,
                    ModelCapability.CODE_ANALYSIS: CapabilityLevel.ADVANCED,
                    ModelCapability.REASONING: CapabilityLevel.ADVANCED,
                    ModelCapability.PLANNING: CapabilityLevel.ADVANCED,
                    ModelCapability.RETRIEVAL: CapabilityLevel.ADVANCED,
                    ModelCapability.SUMMARIZATION: CapabilityLevel.ADVANCED,
                    ModelCapability.DEBUGGING: CapabilityLevel.ADVANCED,
                    ModelCapability.REFACTORING: CapabilityLevel.ADVANCED,
                    ModelCapability.MULTILINGUAL: CapabilityLevel.ADVANCED,
                    ModelCapability.TOOL_USE: CapabilityLevel.ADVANCED,
                }
            elif "haiku" in model_id:
                capabilities = {
                    ModelCapability.CODE_GENERATION: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.CODE_ANALYSIS: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.REASONING: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.PLANNING: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.RETRIEVAL: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.SUMMARIZATION: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.DEBUGGING: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.REFACTORING: CapabilityLevel.INTERMEDIATE,
                    ModelCapability.MULTILINGUAL: CapabilityLevel.INTERMEDIATE,
                }
        
        elif provider == "ollama":
            # Default capabilities for local models
            capabilities = {
                ModelCapability.CODE_GENERATION: CapabilityLevel.INTERMEDIATE,
                ModelCapability.CODE_ANALYSIS: CapabilityLevel.INTERMEDIATE,
                ModelCapability.REASONING: CapabilityLevel.INTERMEDIATE,
                ModelCapability.PLANNING: CapabilityLevel.INTERMEDIATE,
                ModelCapability.RETRIEVAL: CapabilityLevel.BASIC,
                ModelCapability.SUMMARIZATION: CapabilityLevel.INTERMEDIATE,
                ModelCapability.DEBUGGING: CapabilityLevel.INTERMEDIATE,
                ModelCapability.REFACTORING: CapabilityLevel.INTERMEDIATE,
            }
        
        return capabilities
