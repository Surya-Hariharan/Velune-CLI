"""Core model type definitions."""

from enum import Enum, IntEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelCapability(str, Enum):
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
    BASIC = 1
    INTERMEDIATE = 2
    ADVANCED = 3
    EXPERT = 4
    CAPABLE = 2
    STRONG = 3
    EXCEPTIONAL = 4


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


class ModelDescriptor(BaseModel):
    """Descriptor for a model."""
    model_config = ConfigDict(populate_by_name=True, extra="allow")

    model_id: str = Field(alias="id")
    provider_id: str = Field(alias="provider")
    display_name: str = Field(alias="name")
    context_length: int = Field(alias="context_window")
    capabilities: Any
    is_local: bool = False
    quantization: str | None = None  # e.g. "Q4_K_M", "Q8_0"
    vram_required_gb: float | None = None
    parameter_count_b: float | None = None
    speed_tier: Literal["fast", "medium", "slow"] = "medium"
    cost_per_1k_tokens: float | None = None  # None for local models
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
