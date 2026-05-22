"""Events → telemetry."""

from typing import Optional
from velune.events.bus.engine import Event


class TelemetryEventHandler:
    """Handles events by updating telemetry."""

    def __init__(self):
        self._event_counts: dict[str, int] = {}

    async def handle_event(self, event: Event) -> None:
        """Handle any event for telemetry."""
        event_type = event.event_type
        self._event_counts[event_type] = self._event_counts.get(event_type, 0) + 1

    def get_event_counts(self) -> dict[str, int]:
        """Get event counts."""
        return self._event_counts.copy()

    def reset_counts(self) -> None:
        """Reset event counts."""
        self._event_counts.clear()
