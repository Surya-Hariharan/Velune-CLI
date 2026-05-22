"""Events → memory updates."""

from typing import Optional
from velune.events.bus.engine import Event
from velune.memory.lifecycle.manager import MemoryLifecycleManager
from velune.memory.episodic.encoder import EpisodicEncoder


class MemoryEventHandler:
    """Handles events by updating memory."""

    def __init__(self, memory_manager: MemoryLifecycleManager):
        self.memory_manager = memory_manager
        self.encoder = EpisodicEncoder()

    async def handle_file_created(self, event: Event) -> None:
        """Handle file created event."""
        working_manager = self.memory_manager.get_working_manager()
        working_manager.add_observation(
            f"File created: {event.data.get('file_path')}",
            importance=0.6,
        )

    async def handle_file_modified(self, event: Event) -> None:
        """Handle file modified event."""
        working_manager = self.memory_manager.get_working_manager()
        working_manager.add_observation(
            f"File modified: {event.data.get('file_path')}",
            importance=0.5,
        )

    async def handle_command_executed(self, event: Event) -> None:
        """Handle command executed event."""
        working_manager = self.memory_manager.get_working_manager()
        working_manager.add_action(
            f"Command executed: {event.data.get('command')}",
            importance=0.7,
        )

    async def handle_task_completed(self, event: Event) -> None:
        """Handle task completed event."""
        episodic_store = self.memory_manager.get_episodic_store()
        
        record = self.encoder.encode_action(
            action_type="task_completion",
            action_data=event.data,
            result="success" if event.data.get("success") else "failure",
            importance=0.8,
        )
        
        episodic_store.add(record)
