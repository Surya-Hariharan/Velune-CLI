"""Core model type definitions."""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from velune._compat import StrEnum


class ModelCapability(StrEnum):
    """Capability categories used by the model router and classifiers."""

    CODE_GENERATION = "code_generation"
    CODE_ANALYSIS = "code_analysis"
    REASONING = "reasoning"
    PLANNING = "planning"
    TOOL_USE = "tool_use"
    DEBUGGING = "debugging"
    REFACTORING = "refactoring"
    SUMMARIZATION = "summarization"
    RETRIEVAL = "retrieval"
    MULTILINGUAL = "multilingual"
    EMBEDDING = "embedding"
    LONG_CONTEXT = "long_context"


class CapabilityLevel(IntEnum):
    """Capability proficiency levels."""

    NONE = 0
    BASIC = 25
    INTERMEDIATE = 50
    ADVANCED = 75
    EXPERT = 100


class ModelCapabilityProfile(BaseModel):
    """Capability profile for a model."""

    coding: CapabilityLevel = CapabilityLevel.NONE
    reasoning: CapabilityLevel = CapabilityLevel.NONE
    planning: CapabilityLevel = CapabilityLevel.NONE
    summarization: CapabilityLevel = CapabilityLevel.NONE
    embedding: CapabilityLevel = CapabilityLevel.NONE
    instruction_following: CapabilityLevel = CapabilityLevel.NONE
    multimodal: CapabilityLevel = CapabilityLevel.NONE
    tool_use: CapabilityLevel = CapabilityLevel.NONE
    long_context: CapabilityLevel = CapabilityLevel.NONE
    vision: CapabilityLevel = CapabilityLevel.NONE


class ModelDescriptor(BaseModel):
    """Descriptor for a model."""

    model_config = ConfigDict(extra="allow")

    model_id: str
    provider_id: str
    display_name: str
    context_length: int
    capabilities: Any
    is_local: bool = False
    quantization: str | None = None  # e.g. "Q4_K_M", "Q8_0"
    vram_required_gb: float | None = None
    parameter_count_b: float | None = None
    speed_tier: Literal["fast", "medium", "slow"] = "medium"
    cost_per_1k_tokens: float | None = None  # None for local models
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    family: str | None = None  # Model family for prompt adaptation (e.g., "qwen", "claude")
    health: str = "unknown"  # "healthy" | "degraded" | "offline" | "unknown"
    location: str | None = None  # e.g. "http://localhost:11434", "/path/to/model.gguf", "cloud"
    last_latency_ms: float | None = None
