"""Core provider type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from velune._compat import StrEnum


class ProviderHealth(StrEnum):
    """Provider health status."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    OFFLINE = "offline"
    UNAUTHORIZED = "unauthorized"
    UNKNOWN = "unknown"
    # No credentials configured for a key-requiring provider. Distinct from
    # UNAVAILABLE ("configured but currently unreachable") — an unconfigured
    # provider isn't broken, it's simply not set up, and must not be polled or
    # warned about the way a real outage is. See ProviderHealthMonitor.
    UNCONFIGURED = "unconfigured"


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
