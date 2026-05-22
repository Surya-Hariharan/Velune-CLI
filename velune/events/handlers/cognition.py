"""Events → workspace cognition updates."""

from typing import Optional
from velune.events.bus.engine import Event
from velune.workspace.cognition.model import LiveCognitionModel


class CognitionEventHandler:
    """Handles events by updating workspace cognition."""

    def __init__(self, cognition_model: LiveCognitionModel):
        self.cognition_model = cognition_model

    async def handle_task_created(self, event: Event) -> None:
        """Handle task created event."""
        self.cognition_model.set_task(event.data.get("task_id"))

    async def handle_task_completed(self, event: Event) -> None:
        """Handle task completed event."""
        self.cognition_model.clear_task()

    async def handle_agent_started(self, event: Event) -> None:
        """Handle agent started event."""
        self.cognition_model.set_metadata(
            f"agent_{event.data.get('agent_id')}",
            {
                "role": event.data.get("agent_role"),
                "task_id": event.data.get("task_id"),
                "status": "running",
            },
        )

    async def handle_agent_completed(self, event: Event) -> None:
        """Handle agent completed event."""
        agent_id = event.data.get("agent_id")
        metadata = self.cognition_model.get_metadata(f"agent_{agent_id}")
        if metadata:
            metadata["status"] = "completed"
            metadata["duration_ms"] = event.data.get("duration_ms")
            self.cognition_model.set_metadata(f"agent_{agent_id}", metadata)
