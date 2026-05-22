"""Events → memory updates."""

from typing import Optional
from velune.events.bus.engine import Event
from velune.memory.lifecycle import MemoryLifecycleCoordinator


class MemoryEventHandler:
    """Handles events by updating memory."""

    def __init__(self, memory_coordinator: MemoryLifecycleCoordinator):
        self.memory_coordinator = memory_coordinator

    async def handle_file_created(self, event: Event) -> None:
        """Handle file created event."""
        working = self.memory_coordinator.consolidator.working
        if working:
            working.update_state(
                "last_created_file",
                event.data.get("file_path")
            )
            working.log_execution_step(
                step="file_created",
                payload={"file_path": event.data.get("file_path")}
            )

    async def handle_file_modified(self, event: Event) -> None:
        """Handle file modified event."""
        working = self.memory_coordinator.consolidator.working
        if working:
            working.update_state(
                "last_modified_file",
                event.data.get("file_path")
            )
            working.log_execution_step(
                step="file_modified",
                payload={"file_path": event.data.get("file_path")}
            )

    async def handle_command_executed(self, event: Event) -> None:
        """Handle command executed event."""
        working = self.memory_coordinator.consolidator.working
        if working:
            working.update_state(
                "last_executed_command",
                event.data.get("command")
            )
            working.log_execution_step(
                step="command_executed",
                payload={"command": event.data.get("command")}
            )

    async def handle_task_completed(self, event: Event) -> None:
        """Handle task completed event."""
        session_id = event.data.get("session_id") or "default"
        episodic = self.memory_coordinator.consolidator.episodic
        if episodic:
            episodic.add_execution_step(
                session_id=session_id,
                step_name="task_completion",
                status="success" if event.data.get("success") else "failure",
                payload=event.data
            )

