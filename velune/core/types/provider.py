"""Core provider type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from velune._compat import StrEnum


class ProviderHealth(StrEnum):
    """Provider health status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
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


@dataclass
class CapabilityManifest:
    """Real-time capability and health manifest for a provider."""

    provider_id: str
    health: ProviderHealth
    available_models: list[Any]
    rate_limit_remaining: int | None = None
    rate_limit_reset_at: float | None = None
    estimated_latency_ms: int = 0
    supports_streaming: bool = False
    supports_tools: bool = False
    is_online: bool = True
    refreshed_at: float = field(default_factory=lambda: __import__("time").time())

    @property
    def is_available(self) -> bool:
        """True if provider is not unavailable."""
        return self.health != ProviderHealth.UNAVAILABLE
