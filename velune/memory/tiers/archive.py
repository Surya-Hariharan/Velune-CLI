"""Long-Term Archive Tier (Tier 5).

Compresses and stores aged episodic and semantic memory frames using
space-efficient serialized gzip archives.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("velune.memory.tiers.archive")


class LongTermArchiveTier:
    """Tier 5: Compressed long-term storage for cold/historical session snapshots."""

    def __init__(self, archive_dir: Path) -> None:
        self.archive_dir = archive_dir
        self.archive_dir.mkdir(parents=True, exist_ok=True)

    def archive_session(
        self,
        session_id: str,
        turns: List[Dict[str, Any]],
        steps: List[Dict[str, Any]],
        facts: Optional[List[Dict[str, Any]]] = None,
    ) -> Path:
        """
        Serialize and compress session memory frames to a local gzip archive.
        """
        archive_path = self.archive_dir / f"session_{session_id}.json.gz"
        
        payload = {
            "session_id": session_id,
            "turns": turns,
            "steps": steps,
            "facts": facts or [],
        }
        
        try:
            json_data = json.dumps(payload, indent=2).encode("utf-8")
            with gzip.open(archive_path, "wb") as f:
                f.write(json_data)
            logger.info("Successfully archived cold memory session %s to %s", session_id, archive_path)
        except Exception as e:
            logger.error("Failed to write compressed archive for session %s: %s", session_id, e)
            
        return archive_path

    def load_archive(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Decompress and retrieve historical session logs.
        """
        archive_path = self.archive_dir / f"session_{session_id}.json.gz"
        if not archive_path.exists():
            logger.warning("No long-term archive found for session %s", session_id)
            return None

        try:
            with gzip.open(archive_path, "rb") as f:
                decompressed = f.read()
            return json.loads(decompressed.decode("utf-8"))
        except Exception as e:
            logger.error("Failed to load/decompress session archive %s: %s", session_id, e)
            return None

    def list_archived_sessions(self) -> List[str]:
        """List all session IDs that have long-term archives available."""
        sessions = []
        for file in self.archive_dir.glob("session_*.json.gz"):
            # Extract session ID from naming pattern: session_{id}.json.gz
            name = file.name
            session_id = name.replace("session_", "").replace(".json.gz", "")
            sessions.append(session_id)
        return sessions
