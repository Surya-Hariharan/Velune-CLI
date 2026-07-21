"""Unified event types and CognitiveBus for Velune system."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("velune.events")

_HISTORY_MAXLEN = 1000


class Event(BaseModel):
    """The central message token in the event bus."""

    event_id: str = Field(default_factory=lambda: f"evt-{uuid.uuid4().hex[:12]}")
    event_type: str
    timestamp: float = Field(default_factory=lambda: datetime.now(tz=timezone.utc).timestamp())
    source: str
    data: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None

    model_config = ConfigDict(frozen=True)


EventHandler = Callable[[Event], None | Any]


class Subscription:
    """Subscription token allowing easy unsubscribe mechanics."""

    def __init__(self, bus: CognitiveBus, event_type: str, handler: EventHandler) -> None:
        self.bus = bus
        self.event_type = event_type
        self.handler = handler

    def unsubscribe(self) -> None:
        """Cancel this subscription."""
        self.bus.unsubscribe(self.event_type, self.handler)


class CognitiveBus:
    """Async event bus supporting wildcard routing, replay, and correlation waiting."""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[EventHandler]] = {}
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._running: bool = False
        self._dispatch_task: asyncio.Task | None = None
        self._history: deque[Event] = deque(maxlen=_HISTORY_MAXLEN)
        self._pending_responses: dict[str, asyncio.Future[Event]] = {}

    async def emit(self, event: Event) -> None:
        """Enqueue/Publish a kernel event to the bus."""
        if len(self._history) >= _HISTORY_MAXLEN:
            oldest = self._history[0]
            logger.debug(
                "Event history at capacity (%d); dropping oldest: %s [%s]",
                _HISTORY_MAXLEN,
                oldest.event_type,
                oldest.event_id,
            )
        self._history.append(event)

        if event.correlation_id and event.correlation_id in self._pending_responses:
            future = self._pending_responses[event.correlation_id]
            if not future.done():
                future.set_result(event)

        if not self._running:
            await self._dispatch_immediate(event)
            return

        await self._queue.put(event)

    async def subscribe(self, event_type: str, handler: EventHandler) -> Subscription:
        """Subscribe a handler to event types (supports wildcards like 'Memory*')."""
        if event_type not in self._subscribers:
            self._subscribers[event_type] = set()
        self._subscribers[event_type].add(handler)
        logger.debug("Subscribed to pattern '%s'", event_type)
        return Subscription(self, event_type, handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove subscriber handler from active pattern list."""
        if event_type in self._subscribers and handler in self._subscribers[event_type]:
            self._subscribers[event_type].remove(handler)
            if not self._subscribers[event_type]:
                del self._subscribers[event_type]
            logger.debug("Unsubscribed from pattern '%s'", event_type)

    async def emit_and_wait(self, event: Event, timeout: float = 5.0) -> Event:
        """Emits an event and suspends execution until a correlating event returns."""
        future: asyncio.Future[Event] = asyncio.get_running_loop().create_future()
        self._pending_responses[event.event_id] = future

        await self.emit(event)

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except TimeoutError:
            logger.error("Timeout waiting for correlation event to emitted ID: %s", event.event_id)
            raise TimeoutError(f"Event correlation timeout for {event.event_id}")
        finally:
            self._pending_responses.pop(event.event_id, None)

    async def replay(self, from_timestamp: datetime) -> AsyncIterator[Event]:
        """Asynchronously stream events in history since from_timestamp."""
        target_timestamp = from_timestamp.timestamp()
        filtered_events = [evt for evt in self._history if evt.timestamp >= target_timestamp]

        sorted_events = sorted(filtered_events, key=lambda x: x.timestamp)
        for event in sorted_events:
            yield event

    async def start(self) -> None:
        """Start the background event dispatch loop."""
        if self._running:
            return
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        logger.info("Kernel Event Bus started.")

    async def stop(self) -> None:
        """Flush the queue and terminate the dispatch loop."""
        self._running = False
        if self._dispatch_task:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                await self._dispatch_immediate(event)
            except Exception as e:
                logger.error("Error processing queue flush event: %s", e)

        logger.info("Kernel Event Bus stopped.")

    async def _dispatch_loop(self) -> None:
        """Background loop reading and routing events from the queue."""
        while self._running:
            try:
                event = await self._queue.get()
                await self._dispatch_immediate(event)
                self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in event bus dispatch loop: %s", e)

    async def _dispatch_immediate(self, event: Event) -> None:
        """Match event type and invoke all registered subscriber handlers."""
        handlers_to_run = self._find_matching_handlers(event.event_type)
        if not handlers_to_run:
            return

        tasks = []
        for handler in handlers_to_run:
            try:
                if asyncio.iscoroutinefunction(handler):
                    tasks.append(self._run_async_handler(handler, event))
                else:
                    handler(event)
            except Exception as e:
                logger.error("Error running synchronous event handler: %s", e)

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_async_handler(self, handler: EventHandler, event: Event) -> None:
        """Helper to run a coroutine handler with clean error capture."""
        try:
            await cast(Awaitable[Any], handler(event))
        except Exception as e:
            logger.error("Error running asynchronous event handler: %s", e)

    def _find_matching_handlers(self, event_type: str) -> list[EventHandler]:
        """Find all subscriber handlers whose pattern matches the event_type."""
        matched: list[EventHandler] = []
        for pattern, handlers in self._subscribers.items():
            if fnmatch.fnmatchcase(event_type, pattern):
                matched.extend(handlers)
        return matched


# Maintain legacy class name alias as fallback compatibility
class EventBus(CognitiveBus):
    """Fallback alias for compatibility with existing modules."""

    pass
