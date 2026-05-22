"""Experience to episodic record encoding."""

from typing import Dict, Any
from datetime import datetime, timedelta
from velune.core.types import MemoryRecord, MemoryType


class EpisodicEncoder:
    """Encodes experiences into episodic memory records."""

    def __init__(self, retention_days: int = 30):
        self.retention_days = retention_days

    def encode_event(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        importance: float = 0.5,
    ) -> MemoryRecord:
        """Encode an event into an episodic memory record."""
        import uuid
        
        content = f"Event: {event_type}\n"
        for key, value in event_data.items():
            content += f"  {key}: {value}\n"
        
        expires_at = datetime.now() + timedelta(days=self.retention_days)
        
        return MemoryRecord(
            id=str(uuid.uuid4()),
            memory_type=MemoryType.EPISODIC,
            content=content,
            importance=importance,
            access_count=0,
            last_accessed=datetime.now(),
            created_at=datetime.now(),
            expires_at=expires_at,
            metadata={"event_type": event_type, **event_data},
        )

    def encode_conversation(
        self,
        role: str,
        content: str,
        context: Dict[str, Any],
        importance: float = 0.6,
    ) -> MemoryRecord:
        """Encode a conversation turn into an episodic memory record."""
        import uuid
        
        record_content = f"Conversation - {role}:\n{content}\n"
        if context:
            record_content += f"Context: {context}\n"
        
        expires_at = datetime.now() + timedelta(days=self.retention_days)
        
        return MemoryRecord(
            id=str(uuid.uuid4()),
            memory_type=MemoryType.EPISODIC,
            content=record_content,
            importance=importance,
            access_count=0,
            last_accessed=datetime.now(),
            created_at=datetime.now(),
            expires_at=expires_at,
            metadata={"role": role, "context": context},
        )

    def encode_action(
        self,
        action_type: str,
        action_data: Dict[str, Any],
        result: str,
        importance: float = 0.7,
    ) -> MemoryRecord:
        """Encode an action into an episodic memory record."""
        import uuid
        
        content = f"Action: {action_type}\n"
        for key, value in action_data.items():
            content += f"  {key}: {value}\n"
        content += f"Result: {result}\n"
        
        expires_at = datetime.now() + timedelta(days=self.retention_days)
        
        return MemoryRecord(
            id=str(uuid.uuid4()),
            memory_type=MemoryType.EPISODIC,
            content=content,
            importance=importance,
            access_count=0,
            last_accessed=datetime.now(),
            created_at=datetime.now(),
            expires_at=expires_at,
            metadata={"action_type": action_type, "result": result, **action_data},
        )
