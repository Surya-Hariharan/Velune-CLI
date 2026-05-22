import asyncio
import pytest
from datetime import datetime, UTC, timedelta
from velune.kernel.bus import CognitiveBus, Subscription
from velune.kernel.schemas import Event as KernelEvent

@pytest.mark.asyncio
async def test_bus_lifecycle_and_emit():
    bus = CognitiveBus()
    assert not bus._running
    
    await bus.start()
    assert bus._running
    
    events_received = []
    
    async def handler(event: KernelEvent):
        events_received.append(event)
        
    sub = await bus.subscribe("test.event", handler)
    assert isinstance(sub, Subscription)
    
    event = KernelEvent(
        event_type="test.event",
        source="test",
        data={"message": "hello"}
    )
    
    await bus.emit(event)
    # Allow some time for background loop dispatch
    await asyncio.sleep(0.1)
    
    assert len(events_received) == 1
    assert events_received[0].data["message"] == "hello"
    
    sub.unsubscribe()
    await bus.emit(event)
    await asyncio.sleep(0.1)
    
    # Count should still be 1 because of unsubscribe
    assert len(events_received) == 1
    
    await bus.stop()
    assert not bus._running

@pytest.mark.asyncio
async def test_bus_wildcard_subscription():
    bus = CognitiveBus()
    await bus.start()
    
    received_memory_events = []
    received_all_events = []
    
    await bus.subscribe("Memory.*", lambda e: received_memory_events.append(e))
    await bus.subscribe("*", lambda e: received_all_events.append(e))
    
    evt1 = KernelEvent(event_type="Memory.Consolidated", source="test", data={"x": 1})
    evt2 = KernelEvent(event_type="Execution.Started", source="test", data={"y": 2})
    
    await bus.emit(evt1)
    await bus.emit(evt2)
    await asyncio.sleep(0.1)
    
    assert len(received_memory_events) == 1
    assert received_memory_events[0].event_type == "Memory.Consolidated"
    
    assert len(received_all_events) == 2
    
    await bus.stop()

@pytest.mark.asyncio
async def test_bus_emit_and_wait():
    bus = CognitiveBus()
    await bus.start()
    
    # Emit an event, wait for a correlated event to come back
    # Let's set up a subscriber that automatically answers any request event
    async def responder(event: KernelEvent):
        if event.event_type == "request":
            response = KernelEvent(
                event_type="response",
                source="responder",
                data={"result": "success"},
                correlation_id=event.event_id  # correlation_id maps to request event_id
            )
            await bus.emit(response)
            
    await bus.subscribe("request", responder)
    
    req_event = KernelEvent(
        event_type="request",
        source="client",
        data={"action": "calculate"}
    )
    
    res_event = await bus.emit_and_wait(req_event, timeout=1.0)
    assert res_event.event_type == "response"
    assert res_event.data["result"] == "success"
    assert res_event.correlation_id == req_event.event_id
    
    await bus.stop()

@pytest.mark.asyncio
async def test_bus_replay():
    bus = CognitiveBus()
    start_time = datetime.now(tz=UTC)
    
    evt1 = KernelEvent(event_type="evt.1", source="test", timestamp=start_time.timestamp())
    evt2 = KernelEvent(event_type="evt.2", source="test", timestamp=(start_time + timedelta(seconds=1)).timestamp())
    
    await bus.emit(evt1)
    await bus.emit(evt2)
    
    replayed = []
    async for e in bus.replay(start_time):
        replayed.append(e)
        
    assert len(replayed) == 2
    assert replayed[0].event_type == "evt.1"
    assert replayed[1].event_type == "evt.2"
