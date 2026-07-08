"""Resource Connector Framework.

Connects Velune to the user's local development environment — Docker, local
PostgreSQL/MySQL, Supabase — behind one connector interface, gated by the same
approval and encrypted-credential machinery the rest of Velune uses.

Public surface:
    ResourceManager / build_default_manager   — registry + lifecycle + auth hub
    ResourceConnector                          — the interface new connectors implement
    ResourcePermission / ResourceResult / …    — shared value types
"""

from __future__ import annotations

from velune.resources.base import (
    AuthorizationRequest,
    DiscoveryHint,
    ResourceCapability,
    ResourceConnector,
    ResourceConnectorError,
    ResourcePermission,
    ResourceResult,
    ResourceState,
    ResourceStatus,
)
from velune.resources.manager import ResourceManager, build_default_manager

__all__ = [
    "AuthorizationRequest",
    "DiscoveryHint",
    "ResourceCapability",
    "ResourceConnector",
    "ResourceConnectorError",
    "ResourceManager",
    "ResourcePermission",
    "ResourceResult",
    "ResourceState",
    "ResourceStatus",
    "build_default_manager",
]
