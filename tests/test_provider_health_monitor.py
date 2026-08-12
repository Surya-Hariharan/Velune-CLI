"""Provider health monitor: unconfigured-vs-unavailable, and warning-flood suppression.

Regression coverage for the "Provider X unavailable for 3 consecutive polls"
spam: the monitor used to poll every standard provider id it could build an
adapter for, including ones with no API key configured at all, and then
logged the same "3 consecutive polls" warning on every single poll thereafter
(never just once). See ``ProviderHealthMonitor._requires_unset_key`` /
``_record_unconfigured`` (the "not configured" branch) and
``_flagged_unavailable`` (the log-once-per-transition branch).
"""

from __future__ import annotations

import logging

import pytest

from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.health_monitor import ProviderHealthMonitor


class _FakeProvider:
    def __init__(self, health: ProviderHealth = ProviderHealth.UNAVAILABLE) -> None:
        self.health = health

    async def health_check(self) -> ProviderHealth:
        return self.health

    async def list_models(self):
        return []

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_streaming=True, supports_function_calling=False)


class _FakeRegistry:
    def __init__(self, providers: dict, available: frozenset = frozenset()) -> None:
        self._providers = providers
        self._available = available

    def get(self, name):
        return self._providers.get(name)

    def check_provider_available(self, name: str) -> bool:
        return name in self._available


@pytest.fixture
def monitor() -> ProviderHealthMonitor:
    return ProviderHealthMonitor(registry=_FakeRegistry({}))


# --- unconfigured vs unavailable ----------------------------------------------


def test_key_requiring_provider_without_credentials_is_unset():
    monitor = ProviderHealthMonitor(registry=_FakeRegistry({}, available=frozenset()))
    assert monitor._requires_unset_key("anthropic") is True


def test_key_requiring_provider_with_credentials_is_not_unset():
    monitor = ProviderHealthMonitor(registry=_FakeRegistry({}, available=frozenset(["anthropic"])))
    assert monitor._requires_unset_key("anthropic") is False


def test_local_provider_never_counts_as_unset_key():
    """Ollama/LM Studio/etc. take no key at all — they must still be polled."""
    monitor = ProviderHealthMonitor(registry=_FakeRegistry({}))
    for provider_id in ("ollama", "lmstudio", "llamacpp", "openai-compat"):
        assert monitor._requires_unset_key(provider_id) is False


def test_record_unconfigured_sets_state_without_a_live_probe(caplog):
    monitor = ProviderHealthMonitor(registry=_FakeRegistry({}))
    with caplog.at_level(logging.WARNING, logger="velune.providers.health_monitor"):
        monitor._record_unconfigured("anthropic")

    manifest = monitor.get_manifest("anthropic")
    assert manifest is not None
    assert manifest.health == ProviderHealth.UNCONFIGURED
    assert not caplog.records, "marking a provider unconfigured must not warn"


def test_record_unconfigured_is_idempotent_and_quiet_on_repeat(caplog):
    monitor = ProviderHealthMonitor(registry=_FakeRegistry({}))
    with caplog.at_level(logging.WARNING, logger="velune.providers.health_monitor"):
        for _ in range(5):
            monitor._record_unconfigured("anthropic")

    assert not caplog.records


# --- warn once per transition, not once per poll -------------------------------


@pytest.mark.asyncio
async def test_unavailable_warns_once_not_on_every_poll(caplog):
    monitor = ProviderHealthMonitor(registry=_FakeRegistry({}))
    provider = _FakeProvider(ProviderHealth.UNAVAILABLE)

    with caplog.at_level(logging.WARNING, logger="velune.providers.health_monitor"):
        for _ in range(6):
            await monitor._health_check_provider("anthropic", provider)

    warnings = [r for r in caplog.records if "consecutive polls" in r.message]
    assert len(warnings) == 1, f"expected exactly one flood warning, got {len(warnings)}"


@pytest.mark.asyncio
async def test_warning_resets_after_recovery_then_warns_again_on_new_outage(caplog):
    monitor = ProviderHealthMonitor(registry=_FakeRegistry({}))
    provider = _FakeProvider(ProviderHealth.UNAVAILABLE)

    with caplog.at_level(logging.WARNING, logger="velune.providers.health_monitor"):
        for _ in range(3):
            await monitor._health_check_provider("anthropic", provider)

        provider.health = ProviderHealth.HEALTHY
        await monitor._health_check_provider("anthropic", provider)

        provider.health = ProviderHealth.UNAVAILABLE
        for _ in range(3):
            await monitor._health_check_provider("anthropic", provider)

    warnings = [r for r in caplog.records if "consecutive polls" in r.message]
    assert len(warnings) == 2, "a genuinely new outage after recovery must warn again"
