"""Append-only event log."""

import json
from pathlib import Path

from velune.events.bus.engine import Event


class EventLog:
    """Append-only event log for persistence."""

    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    async def append(self, event: Event) -> None:
        """Append an event to the log."""
        log_entry = {
            "event_type": event.event_type,
            "data": event.data,
            "timestamp": event.timestamp,
            "source": event.source,
        }

        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(log_entry) + "\n")

    async def read_all(self) -> list[Event]:
        """Read all events from the log."""
        events = []

        if not self.log_path.exists():
            return events

        with open(self.log_path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    data = json.loads(line)
                    events.append(
                        Event(
                            event_type=data["event_type"],
                            data=data["data"],
                            timestamp=data["timestamp"],
                            source=data["source"],
                        )
                    )

        return events

    async def clear(self) -> None:
        """Clear the event log."""
        if self.log_path.exists():
            self.log_path.unlink()
