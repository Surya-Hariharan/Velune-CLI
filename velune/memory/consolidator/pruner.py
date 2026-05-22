"""Memory pruning and archiving."""

from typing import list
from pathlib import Path
import json
from velune.memory.consolidator.decay import DecayModel
from velune.core.types import MemoryRecord


class MemoryPruner:
    """Prunes and archives memory records."""

    def __init__(self, archive_path: Path):
        self.archive_path = archive_path
        self.archive_path.mkdir(parents=True, exist_ok=True)
        self.decay_model = DecayModel()

    def prune_episodic(
        self,
        records: list[MemoryRecord],
        threshold: float = 0.1,
    ) -> tuple[list[MemoryRecord], list[MemoryRecord]]:
        """Prune episodic memory records."""
        to_keep = []
        to_archive = []
        
        for record in records:
            if self.decay_model.should_prune(record, threshold):
                to_archive.append(record)
            else:
                to_keep.append(record)
        
        return to_keep, to_archive

    def archive_records(self, records: list[MemoryRecord]) -> None:
        """Archive records to disk."""
        archive_file = self.archive_path / f"archive_{int(time.time())}.json"
        
        archived_data = []
        for record in records:
            archived_data.append({
                "id": record.id,
                "memory_type": record.memory_type.value,
                "content": record.content,
                "importance": record.importance,
                "created_at": record.created_at.isoformat(),
                "metadata": record.metadata,
            })
        
        with open(archive_file, "w") as f:
            json.dump(archived_data, f, indent=2)
