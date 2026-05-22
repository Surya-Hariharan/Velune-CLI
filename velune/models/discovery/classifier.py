"""Heuristic model classifier used during discovery."""

from __future__ import annotations

from typing import Any

from velune.core.types import CapabilityLevel, ModelCapability
from velune.models.discovery.schemas import ModelClassification, ModelSpecialization


class ModelClassifier:
    """Classifies models by name, metadata, and provider signals."""

    coding_markers = ("coder", "code", "codestral", "deepseek-coder", "qwen-coder", "starcoder")
    reasoning_markers = ("reason", "r1", "opus", "sonnet", "qwq", "thinking")
    embedding_markers = ("embed", "embedding", "e5", "bge", "gte", "nomic", "mxbai")
    summarization_markers = ("summar", "scribe", "mini", "flash")

    def classify(self, model_id: str, provider_id: str, metadata: dict[str, Any] | None = None) -> ModelClassification:
        metadata = metadata or {}
        model_name = model_id.lower()
        provider_name = provider_id.lower()

        classification = ModelClassification()
        classification.context_length = self._context_length(model_name, metadata)
        classification.speed_tier = self._speed_tier(model_name, metadata)
        classification.embedding_supported = self._is_embedding_model(model_name, provider_name, metadata)
        classification.specialization = self._specialization(model_name, classification.embedding_supported)
        classification.reasoning_quality = self._quality_score(model_name, self.reasoning_markers)
        classification.coding_quality = self._quality_score(model_name, self.coding_markers)
        classification.capabilities = self._capabilities(model_name, classification)
        return classification

    def _capabilities(self, model_name: str, classification: ModelClassification) -> dict[ModelCapability, CapabilityLevel]:
        capabilities: dict[ModelCapability, CapabilityLevel] = {}

        if classification.coding_quality >= 0.75:
            capabilities[ModelCapability.CODE_GENERATION] = CapabilityLevel.EXPERT
            capabilities[ModelCapability.CODE_ANALYSIS] = CapabilityLevel.EXPERT
            capabilities[ModelCapability.DEBUGGING] = CapabilityLevel.ADVANCED
            capabilities[ModelCapability.REFACTORING] = CapabilityLevel.ADVANCED
        elif classification.coding_quality >= 0.45:
            capabilities[ModelCapability.CODE_GENERATION] = CapabilityLevel.ADVANCED
            capabilities[ModelCapability.CODE_ANALYSIS] = CapabilityLevel.ADVANCED
            capabilities[ModelCapability.DEBUGGING] = CapabilityLevel.CAPABLE
            capabilities[ModelCapability.REFACTORING] = CapabilityLevel.CAPABLE

        if classification.reasoning_quality >= 0.75:
            capabilities[ModelCapability.REASONING] = CapabilityLevel.EXPERT
            capabilities[ModelCapability.PLANNING] = CapabilityLevel.EXPERT
        elif classification.reasoning_quality >= 0.45:
            capabilities[ModelCapability.REASONING] = CapabilityLevel.ADVANCED
            capabilities[ModelCapability.PLANNING] = CapabilityLevel.ADVANCED

        if classification.embedding_supported:
            capabilities[ModelCapability.EMBEDDING] = CapabilityLevel.EXPERT

        if any(marker in model_name for marker in self.summarization_markers):
            capabilities[ModelCapability.SUMMARIZATION] = CapabilityLevel.ADVANCED

        if classification.context_length >= 32000:
            capabilities[ModelCapability.LONG_CONTEXT] = CapabilityLevel.ADVANCED

        capabilities[ModelCapability.TOOL_USE] = CapabilityLevel.ADVANCED if classification.reasoning_quality >= 0.45 else CapabilityLevel.CAPABLE
        return capabilities

    def _specialization(self, model_name: str, embedding_supported: bool) -> ModelSpecialization:
        if embedding_supported:
            return ModelSpecialization.EMBEDDING
        if any(marker in model_name for marker in self.coding_markers):
            return ModelSpecialization.CODING
        if any(marker in model_name for marker in self.reasoning_markers):
            return ModelSpecialization.REASONING
        if any(marker in model_name for marker in self.summarization_markers):
            return ModelSpecialization.SUMMARIZATION
        return ModelSpecialization.GENERAL

    def _quality_score(self, model_name: str, markers: tuple[str, ...]) -> float:
        hits = sum(1 for marker in markers if marker in model_name)
        if hits == 0:
            return 0.2
        if hits == 1:
            return 0.65
        return 0.9

    def _is_embedding_model(self, model_name: str, provider_name: str, metadata: dict[str, Any]) -> bool:
        if any(marker in model_name for marker in self.embedding_markers):
            return True
        if metadata.get("embedding", False):
            return True
        return provider_name in {"embedding", "vector"}

    def _context_length(self, model_name: str, metadata: dict[str, Any]) -> int:
        if isinstance(metadata.get("context_length"), int):
            return metadata["context_length"]
        if "128k" in model_name or "128000" in model_name:
            return 128000
        if "32k" in model_name or "32000" in model_name:
            return 32000
        if "16k" in model_name or "16000" in model_name:
            return 16000
        if "8k" in model_name:
            return 8192
        return 4096

    def _speed_tier(self, model_name: str, metadata: dict[str, Any]) -> str:
        size_hint = str(metadata.get("quantization", "")).lower()
        if any(tag in model_name for tag in ("nano", "mini", "small", "fast")):
            return "fast"
        if any(tag in size_hint for tag in ("q4", "q5")):
            return "fast"
        if any(tag in model_name for tag in ("large", "70b", "405b", "opus")):
            return "slow"
        return "medium"