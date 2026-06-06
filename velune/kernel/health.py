"""Subsystem health monitoring and status collection."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from velune.kernel.lifecycle import ComponentStatus, LifecycleCoordinator
from velune.kernel.schemas import HealthReport

logger = logging.getLogger("velune.kernel.health")


class SubsystemHealthMonitor:
    """Monitors system-wide component health, API response, and storage status."""

    def __init__(self, coordinator: LifecycleCoordinator) -> None:
        self._coordinator = coordinator
        self._custom_checks: dict[str, Callable[[], dict]] = {}

    def register_health_hook(self, name: str, hook: Callable[[], dict]) -> None:
        """Register a custom diagnostic function for a subsystem."""
        self._custom_checks[name] = hook

    def check_subsystem(self, name: str) -> HealthReport:
        """Evaluate and report a single subsystem's status and latency."""
        status = self._coordinator.get_status(name)

        start_time = time.perf_counter()
        details = {}

        if name in self._custom_checks:
            try:
                details = self._custom_checks[name]()
            except Exception as e:
                logger.error("Health hook failed for subsystem '%s': %s", name, e)
                details = {"error": str(e)}
                status = ComponentStatus.DEGRADED

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        return HealthReport(
            status=status,
            latency_ms=latency_ms,
            details=details,
        )

    def check_all(self) -> dict[str, HealthReport]:
        """Aggregate health reports for all managed subsystems."""
        reports = {}
        for name in self._coordinator._components.keys():
            reports[name] = self.check_subsystem(name)
        return reports
