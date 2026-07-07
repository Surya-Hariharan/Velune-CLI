"""Core inference type definitions."""

from typing import Any

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """A single tool invocation requested by the model.

    Normalized across providers to the OpenAI shape: ``arguments`` is always a
    parsed dict (adapters are responsible for JSON-decoding string arguments
    and for synthesizing an ``id`` when the provider does not supply one).
    """

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class InferenceRequest(BaseModel):
    """Request for model inference.

    ``messages`` uses the OpenAI chat wire format as Velune's internal normal
    form. Plain turns are ``{"role", "content"}``; tool-calling turns may add
    ``tool_calls`` (assistant) or ``tool_call_id`` (role ``tool``). Adapters
    for non-OpenAI providers translate at the wire boundary.
    """

    model_id: str
    # dict[str, Any] (not dict[str, str]) so assistant tool_calls entries and
    # tool-result messages are representable. Plain string-only messages
    # remain valid — fully backward-compatible.
    messages: list[dict[str, Any]]
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int | None = None
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    stop_sequences: list[str] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Cache hints: maps message index → cache type string.
    # -1 = system message, 0+ = non-system messages[index].
    # None means no caching requested (default, fully backward-compatible).
    cache_hints: dict[int, str] | None = None
    # Tool definitions in OpenAI function format:
    #   {"type": "function", "function": {"name", "description", "parameters"}}
    # None (default) means tool calling is not requested — providers that do
    # not support it never see the field, so this is backward-compatible.
    tools: list[dict[str, Any]] | None = None
    # "auto" | "none" | "required" | {"type": "function", "function": {"name": ...}}
    tool_choice: str | dict[str, Any] | None = None


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
    # Cache token counts — populated by providers that support prompt caching.
    # Defaults to 0 for all providers that do not support caching (backward-compatible).
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # Tool calls requested by the model. None when the turn is plain text or
    # the provider/adapters do not support tool calling (backward-compatible).
    # When set, ``finish_reason`` is normalized to "tool_calls".
    tool_calls: list[ToolCall] | None = None
