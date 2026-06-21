"""Tests for ProactiveWatcher and AlertStore."""

from __future__ import annotations

import asyncio

import pytest

from velune.proactive.alerts import Alert, AlertSeverity, AlertStore, make_alert
from velune.proactive.watcher import ProactiveWatcher


class TestAlertStore:
    def setup_method(self):
        self.store = AlertStore()

    def _alert(self, severity=AlertSeverity.WARN, title="test", body="body"):
        return make_alert(severity, title, body, "test")

    def test_add_increments_unread(self):
        self.store.add(self._alert())
        assert self.store.unread_count() == 1

    def test_drain_unread_returns_and_clears(self):
        self.store.add(self._alert())
        self.store.add(self._alert())
        unread = self.store.drain_unread()
        assert len(unread) == 2
        assert self.store.unread_count() == 0

    def test_drain_marks_acknowledged(self):
        self.store.add(self._alert())
        self.store.drain_unread()
        # Second drain returns nothing
        assert self.store.drain_unread() == []

    def test_all_alerts_returns_all(self):
        for _ in range(5):
            self.store.add(self._alert())
        assert len(self.store.all_alerts()) == 5

    def test_max_alerts_cap(self):
        for _ in range(25):
            self.store.add(self._alert())
        assert len(self.store.all_alerts()) == AlertStore.MAX_ALERTS

    def test_clear(self):
        self.store.add(self._alert())
        self.store.clear()
        assert self.store.unread_count() == 0
        assert self.store.all_alerts() == []


class FakeEvent:
    def __init__(self, event_type: str, data: dict):
        self.event_type = event_type
        self.data = data


class FakeBus:
    async def subscribe(self, event_type, handler):
        class Sub:
            def unsubscribe(self):
                pass

        return Sub()


class FakeStatusState:
    def __init__(self):
        self.alert_count = 0


@pytest.mark.asyncio
class TestProactiveWatcher:
    async def _make_watcher(self):
        store = AlertStore()
        status = FakeStatusState()
        watcher = ProactiveWatcher(
            bus=FakeBus(),
            alert_store=store,
            job_registry=None,
            health_monitor=None,
            status_state=status,
        )
        await watcher.start()
        return watcher, store, status

    async def test_start_stop(self):
        watcher, _, _ = await self._make_watcher()
        assert watcher._running is True
        await watcher.stop()
        assert watcher._running is False

    async def test_on_job_failed_creates_warn_alert(self):
        watcher, store, status = await self._make_watcher()
        event = FakeEvent("job.failed", {"job_id": "job-0001", "error": "something broke"})
        await watcher._on_job_failed(event)
        await watcher.stop()

        alerts = store.all_alerts()
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.WARN
        assert "job-0001" in alerts[0].title
        assert status.alert_count == 1

    async def test_on_loop_detected_creates_danger_alert(self):
        watcher, store, status = await self._make_watcher()
        event = FakeEvent(
            "retry.loop_detected",
            {"error_type": "RuntimeError", "error_preview": "boom", "occurrences": 3},
        )
        await watcher._on_loop_detected(event)
        await watcher.stop()

        alerts = store.all_alerts()
        assert alerts[0].severity == AlertSeverity.DANGER
        assert status.alert_count == 1

    async def test_on_provider_health_degraded_creates_warn(self):
        watcher, store, _ = await self._make_watcher()
        event = FakeEvent(
            "provider.health_changed", {"provider_id": "openai", "health": "degraded"}
        )
        await watcher._on_provider_health(event)
        await watcher.stop()

        assert store.all_alerts()[0].severity == AlertSeverity.WARN

    async def test_on_provider_health_unavailable_creates_danger(self):
        watcher, store, _ = await self._make_watcher()
        event = FakeEvent(
            "provider.health_changed", {"provider_id": "anthropic", "health": "unavailable"}
        )
        await watcher._on_provider_health(event)
        await watcher.stop()

        assert store.all_alerts()[0].severity == AlertSeverity.DANGER

    async def test_on_context_threshold_70_creates_warn(self):
        watcher, store, _ = await self._make_watcher()
        event = FakeEvent("context.threshold_crossed", {"pct": 75.0, "threshold": 70})
        await watcher._on_context_threshold(event)
        await watcher.stop()

        assert store.all_alerts()[0].severity == AlertSeverity.WARN

    async def test_on_context_threshold_90_creates_danger(self):
        watcher, store, _ = await self._make_watcher()
        event = FakeEvent("context.threshold_crossed", {"pct": 92.0, "threshold": 90})
        await watcher._on_context_threshold(event)
        await watcher.stop()

        assert store.all_alerts()[0].severity == AlertSeverity.DANGER

    async def test_status_state_alert_count_updated(self):
        watcher, store, status = await self._make_watcher()
        event = FakeEvent("job.failed", {"job_id": "job-0001", "error": "err"})
        await watcher._on_job_failed(event)
        await watcher._on_job_failed(event)
        await watcher.stop()

        assert status.alert_count == 2
