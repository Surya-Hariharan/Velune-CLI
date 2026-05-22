"""Schemas for model discovery and registry."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from velune.core.types import CapabilityLevel, ModelCapability, ModelDescriptor


class ModelSpecialization(str, Enum):
    """High-level model specialization categories."""

    GENERAL = "general"
    CODING = "coding"
    REASONING = "reasoning"
    EMBEDDING = "embedding"
    SUMMARIZATION = "summarization"
    MULTIMODAL = "multimodal"


class DiscoverySource(str, Enum):
    """Where a model record was discovered."""

    OLLAMA = "ollama"
    LM_STUDIO = "lm_studio"
    GGUF = "gguf"
    HUGGINGFACE = "huggingface"


class ModelClassification(BaseModel):
    """Derived capabilities and operational characteristics."""

    specialization: ModelSpecialization = ModelSpecialization.GENERAL
    speed_tier: str = "medium"
    embedding_supported: bool = False
    reasoning_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    coding_quality: float = Field(default=0.0, ge=0.0, le=1.0)
    context_length: int = 4096
    capabilities: dict[ModelCapability, CapabilityLevel] = Field(default_factory=dict)


class ModelRecord(BaseModel):
    """Discovered model plus its provenance and derived traits."""

    descriptor: ModelDescriptor
    source: DiscoverySource
    classification: ModelClassification = Field(default_factory=ModelClassification)
    location: Optional[str] = None
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def model_id(self) -> str:
        return self.descriptor.model_id

    @property
    def provider_id(self) -> str:
        return self.descriptor.provider_id