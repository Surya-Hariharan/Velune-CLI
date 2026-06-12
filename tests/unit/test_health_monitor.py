"""Unit tests for ProviderHealthMonitor."""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from velune.core.types.provider import CapabilityManifest, ProviderHealth
from velune.providers.health_monitor import ProviderHealthMonitor
from velune.providers.registry import ProviderRegistry


@pytest.fixture
def mock_provider():
    """Create a mock provider."""
    provider = AsyncMock()
    provider.provider_id = "mock_provider"
    provider.health_check = AsyncMock(return_value=ProviderHealth.HEALTHY)
    provider.list_models = AsyncMock(return_value=[])
    provider.get_capabilities = MagicMock()
    provider.get_capabilities.return_value.supports_streaming = True
    provider.get_capabilities.return_value.supports_function_calling = True
    return provider


@pytest.fixture
def mock_registry(mock_provider):
    """Create a mock registry."""
    registry = MagicMock(spec=ProviderRegistry)
    registry.get = MagicMock(side_effect=lambda name: mock_provider if name == "mock_provider" else None)
    return registry


@pytest.mark.asyncio
async def test_health_monitor_initialization(mock_registry):
    """Test that ProviderHealthMonitor initializes correctly."""
    monitor = ProviderHealthMonitor(mock_registry)
    assert monitor is not None
    assert monitor._running is False


@pytest.mark.asyncio
async def test_health_monitor_start_stop(mock_registry):
    """Test health monitor start and stop."""
    monitor = ProviderHealthMonitor(mock_registry)

    await monitor.start()
    assert monitor._running is True

    await asyncio.sleep(0.1)  # Let polling loop run briefly

    await monitor.stop()
    assert monitor._running is False


@pytest.mark.asyncio
async def test_manifest_recording(mock_registry):
    """Test that health monitor records manifests."""
    monitor = ProviderHealthMonitor(mock_registry)

    await monitor._health_check_provider("mock_provider", mock_registry.get("mock_provider"))

    manifest = monitor.get_manifest("mock_provider")
    assert manifest is not None
    assert manifest.provider_id == "mock_provider"
    assert manifest.health == ProviderHealth.HEALTHY


def test_latency_recording(mock_registry):
    """Test latency recording and rolling average."""
    monitor = ProviderHealthMonitor(mock_registry)

    # Record some latencies
    monitor.record_latency("test_provider", 100)
    monitor.record_latency("test_provider", 150)
    monitor.record_latency("test_provider", 200)

    # Check that latency window was updated
    assert "test_provider" in monitor._latency_windows
    assert len(monitor._latency_windows["test_provider"]) == 3
    assert list(monitor._latency_windows["test_provider"]) == [100, 150, 200]


def test_capability_manifest_is_available(mock_registry):
    """Test CapabilityManifest.is_available property."""
    manifest_healthy = CapabilityManifest(
        provider_id="test",
        health=ProviderHealth.HEALTHY,
        available_models=[]
    )
    assert manifest_healthy.is_available is True

    manifest_degraded = CapabilityManifest(
        provider_id="test",
        health=ProviderHealth.DEGRADED,
        available_models=[]
    )
    assert manifest_degraded.is_available is True

    manifest_unavailable = CapabilityManifest(
        provider_id="test",
        health=ProviderHealth.UNAVAILABLE,
        available_models=[]
    )
    assert manifest_unavailable.is_available is False


def test_get_all_manifests(mock_registry):
    """Test getting all manifests."""
    monitor = ProviderHealthMonitor(mock_registry)

    manifest1 = CapabilityManifest(
        provider_id="provider1",
        health=ProviderHealth.HEALTHY,
        available_models=[]
    )
    manifest2 = CapabilityManifest(
        provider_id="provider2",
        health=ProviderHealth.DEGRADED,
        available_models=[]
    )

    monitor._manifests["provider1"] = manifest1
    monitor._manifests["provider2"] = manifest2

    all_manifests = monitor.get_all_manifests()
    assert len(all_manifests) == 2
    assert all_manifests["provider1"] == manifest1
    assert all_manifests["provider2"] == manifest2
