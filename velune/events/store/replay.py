"""Event replay for debugging."""

import asyncio
from typing import Optional, Callable
from velune.events.bus.engine import Event
from velune.events.store.log import EventLog


class EventReplayer:
    """Replays events from the event log."""

    def __init__(self, event_log: EventLog):
        self.event_log = event_log

    async def replay_all(self, handler: Callable[[Event], None]) -> None:
        """Replay all events from the log."""
        events = await self.event_log.read_all()
        
        for event in events:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception:
                pass

    async def replay_since(self, timestamp: float, handler: Callable[[Event], None]) -> None:
        """Replay events since a timestamp."""
        events = await self.event_log.read_all()
        
        for event in events:
            if event.timestamp >= timestamp:
                try:
                    if asyncio.iscoroutinefunction(handler):
                        await handler(event)
                    else:
                        handler(event)
                except Exception:
                    pass
