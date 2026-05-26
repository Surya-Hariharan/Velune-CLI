"""Event replay for debugging."""

import asyncio
from collections.abc import Callable

from velune.kernel.schemas import Event as KernelEvent
from velune.events.store.log import EventLog


class EventReplayer:
    """Replays events from the event log."""

    def __init__(self, event_log: EventLog):
        self.event_log = event_log

    async def replay_all(self, handler: Callable[[KernelEvent], None]) -> None:
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

    async def replay_since(self, timestamp: float, handler: Callable[[KernelEvent], None]) -> None:
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
