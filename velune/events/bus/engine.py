"""Async event bus (asyncio-based)."""

import asyncio
from typing import Callable, Dict, Any, Optional
from dataclasses import dataclass


@dataclass
class Event:
    """Base event class."""
    event_type: str
    data: Dict[str, Any]
    timestamp: float
    source: str


class EventBus:
    """Async event bus for event-driven cognition."""

    def __init__(self):
        self._subscribers: Dict[str, list[Callable]] = {}
        self._running = False
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

    def subscribe(self, event_type: str, handler: Callable[[Event], None]) -> None:
        """Subscribe to an event type."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = []
        self._subscribers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Callable[[Event], None]) -> None:
        """Unsubscribe from an event type."""
        if event_type in self._subscribers:
            self._subscribers[event_type].remove(handler)

    async def publish(self, event: Event) -> None:
        """Publish an event to the bus."""
        await self._queue.put(event)

    async def start(self) -> None:
        """Start the event bus."""
        if self._running:
            return
        
        self._running = True
        self._task = asyncio.create_task(self._process_events())

    async def stop(self) -> None:
        """Stop the event bus."""
        self._running = False
        
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _process_events(self) -> None:
        """Process events from the queue."""
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._handle_event(event)
            except asyncio.TimeoutError:
                continue
            except Exception:
                pass

    async def _handle_event(self, event: Event) -> None:
        """Handle a single event."""
        handlers = self._subscribers.get(event.event_type, [])
        
        for handler in handlers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(event)
                else:
                    handler(event)
            except Exception:
                pass
