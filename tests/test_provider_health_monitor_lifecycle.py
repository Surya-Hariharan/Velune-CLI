"""ProviderHealthMonitor's background poller must actually start and stop.

Previously ``ProviderHealthMonitor`` was constructed by
``providers/subsystems.py`` but never had ``.start()`` called anywhere in the
codebase — /doctor's health table and ProviderRouter's health-based
filtering always read an empty manifest map. It's now registered with a
``lifecycle_key`` so ``LifecycleCoordinator`` drives it via the standard
``initialize()``/``shutdown()`` hooks, at a 5-minute interval (not the
original 30s, which would poll every configured cloud provider over the
network continuously).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from velune.providers.health_monitor import ProviderHealthMonitor
from velune.providers.subsystems import PROVIDER_MODULES


def _monitor(**kwargs) -> ProviderHealthMonitor:
    registry = MagicMock()
    registry.get.return_value = None  # no providers registered — loop body is a no-op
    return ProviderHealthMonitor(registry, **kwargs)


async def test_initialize_starts_the_polling_task():
    monitor = _monitor()
    assert monitor._running is False

    await monitor.initialize()

    assert monitor._running is True
    assert monitor._polling_task is not None
    assert not monitor._polling_task.done()

    await monitor.shutdown()


async def test_shutdown_stops_the_polling_task():
    monitor = _monitor()
    await monitor.initialize()

    await monitor.shutdown()

    assert monitor._running is False
    assert monitor._polling_task.cancelled() or monitor._polling_task.done()


async def test_default_poll_interval_is_conservative_not_30_seconds():
    """The original 30s default meant continuous background API traffic
    against every configured cloud provider purely to populate a table
    nothing consumed. 300s (5 min) is the wired-in default now."""
    monitor = _monitor()
    assert monitor._poll_interval == 300.0


async def test_custom_poll_interval_is_honored():
    monitor = _monitor(poll_interval=60.0)
    assert monitor._poll_interval == 60.0


def test_health_monitor_module_is_lifecycle_managed():
    module = next(m for m in PROVIDER_MODULES if m.container_key == "runtime.provider_health_monitor")
    assert module.lifecycle_key == "provider_health_monitor"


async def test_initialize_is_idempotent_when_already_running():
    """A double-initialize (e.g. a re-entrant lifecycle.startup() call) must
    not raise or spawn a second polling task."""
    monitor = _monitor()
    await monitor.initialize()
    first_task = monitor._polling_task

    await monitor.initialize()

    assert monitor._polling_task is first_task
    await monitor.shutdown()
