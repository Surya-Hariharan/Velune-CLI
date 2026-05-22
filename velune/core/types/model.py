"""Core model type definitions."""

from enum import IntEnum
from typing import Any, Optional, Literal
from pydantic import BaseModel, Field


class CapabilityLevel(IntEnum):
    """Capability proficiency levels."""
    NONE = 0
    BASIC = 1
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
    model_id: str
    provider_id: str
    display_name: str
    context_length: int
    capabilities: ModelCapabilityProfile
    quantization: Optional[str] = None  # e.g. "Q4_K_M", "Q8_0"
    vram_required_gb: Optional[float] = None
    parameter_count_b: Optional[float] = None
    speed_tier: Literal["fast", "medium", "slow"] = "medium"
    cost_per_1k_tokens: Optional[float] = None  # None for local models
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
