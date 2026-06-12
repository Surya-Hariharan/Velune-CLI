"""Core inference type definitions."""

from typing import Any

from pydantic import BaseModel, Field


class InferenceRequest(BaseModel):
    """Request for model inference."""

    model_id: str
    messages: list[dict[str, str]]
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = None
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    stop_sequences: list[str] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class StreamChunk(BaseModel):
    """A chunk of streamed inference response."""

    content: str
    finish_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class InferenceResponse(BaseModel):
    """Response from model inference."""

    content: str
    model_id: str
    finish_reason: str
    tokens_used: int
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
