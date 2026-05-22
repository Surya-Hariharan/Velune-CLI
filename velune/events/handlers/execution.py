"""Handles system execution events, updating telemetry log traces and memories."""

from __future__ import annotations

import logging
from typing import Dict, Any

from velune.kernel.schemas import Event as KernelEvent
from velune.kernel.bus import CognitiveBus

logger = logging.getLogger("velune.events.handlers.execution")


class ExecutionEventHandler:
    """Subscriber that listens to active execution lifecycle events on the CognitiveBus."""

    def __init__(self, bus: CognitiveBus) -> None:
        self.bus = bus

    async def register_subscriptions(self) -> None:
        """Register subscriptions for all execution-related events."""
        await self.bus.subscribe("PlanCreated", self.handle_plan_created)
        await self.bus.subscribe("StepStarted", self.handle_step_started)
        await self.bus.subscribe("StepCompleted", self.handle_step_completed)
        await self.bus.subscribe("StepFailed", self.handle_step_failed)
        await self.bus.subscribe("RollbackTriggered", self.handle_rollback_triggered)

    async def handle_plan_created(self, event: KernelEvent) -> None:
        """Logs plan creation metrics."""
        task_id = event.data.get("task_id", "unknown")
        steps_count = len(event.data.get("steps", []))
        logger.info("[EVENT] PlanCreated for Task %s containing %d steps", task_id, steps_count)

    async def handle_step_started(self, event: KernelEvent) -> None:
        """Logs step start metrics."""
        step_id = event.data.get("step_id", "unknown")
        logger.info("[EVENT] StepStarted: %s", step_id)

    async def handle_step_completed(self, event: KernelEvent) -> None:
        """Logs step success metrics."""
        step_id = event.data.get("step_id", "unknown")
        duration = event.data.get("duration_ms", 0.0)
        logger.info("[EVENT] StepCompleted: %s in %.2fms", step_id, duration)

    async def handle_step_failed(self, event: KernelEvent) -> None:
        """Logs step failure metrics."""
        step_id = event.data.get("step_id", "unknown")
        error = event.data.get("error", "")
        logger.error("[EVENT] StepFailed: %s. Error: %s", step_id, error)

    async def handle_rollback_triggered(self, event: KernelEvent) -> None:
        """Logs state rollbacks."""
        checkpoint_id = event.data.get("checkpoint_id", "unknown")
        logger.warning("[EVENT] RollbackTriggered for checkpoint %s", checkpoint_id)
