import pytest
import time
from pathlib import Path
from velune.events.bus.engine import Event
from velune.events.store.log import EventLog
from velune.events.store.replay import EventReplayer

@pytest.mark.asyncio
async def test_event_replayer(tmp_path: Path):
    log_file = tmp_path / "events.jsonl"
    event_log = EventLog(log_file)
    
    # 1. Create one Event
    test_event = Event(
        event_type="test.event",
        data={"message": "hello"},
        timestamp=time.time(),
        source="unit_test"
    )
    
    # 2. Append to log
    await event_log.append(test_event)
    
    # 3. Setup event replayer
    replayer = EventReplayer(event_log)
    
    # 4. Define sync handler to receive event
    received_events = []
    def sync_handler(event: Event) -> None:
        received_events.append(event)
        
    # 5. Replay all
    await replayer.replay_all(sync_handler)
    
    # 6. Verify received event
    assert len(received_events) == 1
    rec_event = received_events[0]
    assert rec_event.event_type == "test.event"
    assert rec_event.data == {"message": "hello"}
    assert rec_event.source == "unit_test"
    
    # Test replay_since
    received_since = []
    def sync_handler_since(event: Event) -> None:
        received_since.append(event)
        
    await replayer.replay_since(test_event.timestamp - 1.0, sync_handler_since)
    assert len(received_since) == 1
