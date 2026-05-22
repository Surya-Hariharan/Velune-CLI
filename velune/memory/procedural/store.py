"""Successful workflow patterns store."""

import json
from typing import Dict, Optional, list
from pathlib import Path
from velune.core.types import MemoryRecord, MemoryType
from velune.core.errors import MemoryStoreError


class ProceduralMemoryStore:
    """Store for procedural memory (workflow patterns)."""

    def __init__(self, storage_path: Path):
        self.storage_path = storage_path
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self._patterns: Dict[str, dict] = {}
        self._load_patterns()

    def _load_patterns(self) -> None:
        """Load patterns from disk."""
        patterns_file = self.storage_path / "patterns.json"
        if patterns_file.exists():
            with open(patterns_file, "r") as f:
                self._patterns = json.load(f)

    def _save_patterns(self) -> None:
        """Save patterns to disk."""
        patterns_file = self.storage_path / "patterns.json"
        with open(patterns_file, "w") as f:
            json.dump(self._patterns, f, indent=2)

    def add(self, record: MemoryRecord) -> None:
        """Add a procedural memory record."""
        if record.memory_type != MemoryType.PROCEDURAL:
            raise MemoryStoreError("Procedural memory store only accepts PROCEDURAL type records")
        
        pattern_id = record.id
        pattern_data = {
            "content": record.content,
            "importance": record.importance,
            "created_at": record.created_at.isoformat(),
            "metadata": record.metadata,
        }
        
        self._patterns[pattern_id] = pattern_data
        self._save_patterns()

    def get(self, pattern_id: str) -> Optional[MemoryRecord]:
        """Get a procedural memory pattern."""
        if pattern_id not in self._patterns:
            return None
        
        data = self._patterns[pattern_id]
        from datetime import datetime
        
        return MemoryRecord(
            id=pattern_id,
            memory_type=MemoryType.PROCEDURAL,
            content=data["content"],
            importance=data["importance"],
            access_count=0,
            last_accessed=datetime.now(),
            created_at=datetime.fromisoformat(data["created_at"]),
            expires_at=None,
            metadata=data.get("metadata", {}),
        )

    def find_similar(self, task_description: str, limit: int = 5) -> list[MemoryRecord]:
        """Find similar procedural patterns."""
        # Simple keyword matching for now
        task_lower = task_description.lower()
        similar = []
        
        for pattern_id, data in self._patterns.items():
            content_lower = data["content"].lower()
            # Check for keyword overlap
            task_words = set(task_lower.split())
            content_words = set(content_lower.split())
            overlap = len(task_words & content_words)
            
            if overlap > 0:
                from datetime import datetime
                similar.append(
                    MemoryRecord(
                        id=pattern_id,
                        memory_type=MemoryType.PROCEDURAL,
                        content=data["content"],
                        importance=data["importance"],
                        access_count=0,
                        last_accessed=datetime.now(),
                        created_at=datetime.fromisoformat(data["created_at"]),
                        expires_at=None,
                        metadata=data.get("metadata", {}),
                    )
                )
        
        # Sort by importance
        similar.sort(key=lambda r: r.importance, reverse=True)
        return similar[:limit]

    def list_all(self) -> list[MemoryRecord]:
        """List all procedural patterns."""
        patterns = []
        for pattern_id in self._patterns:
            pattern = self.get(pattern_id)
            if pattern:
                patterns.append(pattern)
        return patterns
