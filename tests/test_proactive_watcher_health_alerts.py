"""`ProactiveWatcher._run_periodic_checks` must not flood the user with an
alert per unconfigured provider on every tick.

Regression coverage for the bug where `/models` (or literally any command —
`poll_and_render_alerts` drains on every prompt submit) appeared to dump a
wall of "Provider X unavailable" panels: the periodic health check alerted
on *every* provider `ProviderRegistry` knows a lazy factory for, including
ones the user never configured a key for, and re-added the same alert every
15s tick forever since it never tracked what it had already reported.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from velune.core.types.provider import CapabilityManifest, ProviderHealth
from velune.proactive.alerts import AlertStore
from velune.proactive.watcher import ProactiveWatcher


def _manifest(provider_id: str, health: ProviderHealth) -> CapabilityManifest:
    return CapabilityManifest(provider_id=provider_id, health=health, available_models=[])


class _FakeHealthMonitor:
    def __init__(self, manifests: dict[str, CapabilityManifest]) -> None:
        self._manifests = manifests

    def get_all_manifests(self) -> dict[str, CapabilityManifest]:
        return dict(self._manifests)


def _make_watcher(manifests: dict[str, CapabilityManifest]) -> ProactiveWatcher:
    return ProactiveWatcher(
        bus=None,
        alert_store=AlertStore(),
        job_registry=None,
        health_monitor=_FakeHealthMonitor(manifests),
    )


@pytest.mark.asyncio
async def test_unconfigured_provider_unavailable_does_not_alert():
    watcher = _make_watcher({"meta": _manifest("meta", ProviderHealth.UNAVAILABLE)})
    with patch("velune.providers.keystore.has_key", return_value=False):
        await watcher._run_periodic_checks()
    assert watcher._store.unread_count() == 0


@pytest.mark.asyncio
async def test_configured_provider_unavailable_alerts_once():
    watcher = _make_watcher({"groq": _manifest("groq", ProviderHealth.UNAVAILABLE)})
    with patch("velune.providers.keystore.has_key", return_value=True):
        await watcher._run_periodic_checks()
    assert watcher._store.unread_count() == 1
    alert = watcher._store.all_alerts()[0]
    assert alert.title == "Provider groq unavailable"


@pytest.mark.asyncio
async def test_keyless_local_provider_unavailable_still_alerts():
    watcher = _make_watcher({"ollama": _manifest("ollama", ProviderHealth.UNAVAILABLE)})
    with patch("velune.providers.keystore.has_key", return_value=False):
        await watcher._run_periodic_checks()
    assert watcher._store.unread_count() == 1


@pytest.mark.asyncio
async def test_still_unavailable_provider_does_not_re_alert_on_next_tick():
    watcher = _make_watcher({"groq": _manifest("groq", ProviderHealth.UNAVAILABLE)})
    with patch("velune.providers.keystore.has_key", return_value=True):
        await watcher._run_periodic_checks()
        await watcher._run_periodic_checks()
        await watcher._run_periodic_checks()
    assert watcher._store.unread_count() == 1, (
        "a provider that stays unavailable across ticks must only be "
        "reported once, not re-queued every 15s forever"
    )


@pytest.mark.asyncio
async def test_recovered_then_failed_again_alerts_a_second_time():
    watcher = _make_watcher({"groq": _manifest("groq", ProviderHealth.UNAVAILABLE)})
    with patch("velune.providers.keystore.has_key", return_value=True):
        await watcher._run_periodic_checks()
        assert watcher._store.unread_count() == 1

        watcher._health_monitor = _FakeHealthMonitor(
            {"groq": _manifest("groq", ProviderHealth.HEALTHY)}
        )
        await watcher._run_periodic_checks()
        watcher._store.drain_unread()

        watcher._health_monitor = _FakeHealthMonitor(
            {"groq": _manifest("groq", ProviderHealth.UNAVAILABLE)}
        )
        await watcher._run_periodic_checks()

    assert watcher._store.unread_count() == 1, "a fresh outage after recovery must alert again"


@pytest.mark.asyncio
async def test_mixed_configured_and_unconfigured_providers_only_alerts_configured():
    watcher = _make_watcher(
        {
            "groq": _manifest("groq", ProviderHealth.UNAVAILABLE),
            "meta": _manifest("meta", ProviderHealth.UNAVAILABLE),
            "nvidia": _manifest("nvidia", ProviderHealth.UNAVAILABLE),
            "ollama": _manifest("ollama", ProviderHealth.HEALTHY),
        }
    )

    def _has_key(provider_id: str) -> bool:
        return provider_id == "groq"

    with patch("velune.providers.keystore.has_key", side_effect=_has_key):
        await watcher._run_periodic_checks()

    assert watcher._store.unread_count() == 1
    assert watcher._store.all_alerts()[0].title == "Provider groq unavailable"


@pytest.mark.asyncio
async def test_healthy_providers_never_alert():
    watcher = _make_watcher({"groq": _manifest("groq", ProviderHealth.HEALTHY)})
    with patch("velune.providers.keystore.has_key", return_value=True):
        await watcher._run_periodic_checks()
    assert watcher._store.unread_count() == 0
