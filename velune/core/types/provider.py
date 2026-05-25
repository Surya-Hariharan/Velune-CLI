"""Core provider type definitions."""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ProviderHealth(str, Enum):
    """Provider health status."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ProviderConfig(BaseModel):
    """Configuration for a provider."""
    name: str
    base_url: str
    api_key: str | None = None
    api_key_env: str | None = None
    timeout: int = Field(default=30, ge=1)
    max_retries: int = Field(default=3, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProviderCapabilities(BaseModel):
    """Capabilities offered by a provider."""
    supports_streaming: bool = False
    supports_function_calling: bool = False
    supports_embeddings: bool = False
    max_context_window: int | None = None
    rate_limit_rpm: int | None = None
    rate_limit_tpm: int | None = None
