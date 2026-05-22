"""SQLite-backed episodic event store."""

import sqlite3
import json
from typing import Optional, list
from pathlib import Path
from datetime import datetime
from velune.core.types import MemoryRecord, MemoryType
from velune.core.errors import MemoryStoreError


class EpisodicMemoryStore:
    """SQLite-backed episodic memory store."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._initialize_db()

    def _initialize_db(self) -> None:
        """Initialize the database schema."""
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS episodic_memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                importance REAL NOT NULL,
                access_count INTEGER DEFAULT 0,
                last_accessed TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                expires_at TIMESTAMP,
                metadata TEXT,
                embedding BLOB
            )
        """)
        self._conn.commit()

    def add(self, record: MemoryRecord) -> None:
        """Add a record to episodic memory."""
        if record.memory_type != MemoryType.EPISODIC:
            raise MemoryStoreError("Episodic memory store only accepts EPISODIC type records")
        
        embedding_blob = None
        if record.embedding:
            import struct
            embedding_blob = struct.pack(f"{len(record.embedding)}f", *record.embedding)
        
        self._conn.execute(
            """
            INSERT INTO episodic_memories 
            (id, content, importance, access_count, last_accessed, created_at, expires_at, metadata, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.content,
                record.importance,
                record.access_count,
                record.last_accessed.isoformat(),
                record.created_at.isoformat(),
                record.expires_at.isoformat() if record.expires_at else None,
                json.dumps(record.metadata),
                embedding_blob,
            ),
        )
        self._conn.commit()

    def get(self, record_id: str) -> Optional[MemoryRecord]:
        """Get a record from episodic memory."""
        cursor = self._conn.execute(
            "SELECT * FROM episodic_memories WHERE id = ?",
            (record_id,),
        )
        row = cursor.fetchone()
        if not row:
            return None
        
        return self._row_to_record(row)

    def get_recent(self, limit: int = 100) -> list[MemoryRecord]:
        """Get recent records from episodic memory."""
        cursor = self._conn.execute(
            "SELECT * FROM episodic_memories ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [self._row_to_record(row) for row in cursor.fetchall()]

    def search_by_content(self, query: str, limit: int = 10) -> list[MemoryRecord]:
        """Search records by content."""
        cursor = self._conn.execute(
            "SELECT * FROM episodic_memories WHERE content LIKE ? LIMIT ?",
            (f"%{query}%", limit),
        )
        return [self._row_to_record(row) for row in cursor.fetchall()]

    def cleanup_expired(self) -> int:
        """Remove expired records."""
        cursor = self._conn.execute(
            "DELETE FROM episodic_memories WHERE expires_at < ?",
            (datetime.now().isoformat(),),
        )
        deleted = cursor.rowcount
        self._conn.commit()
        return deleted

    def _row_to_record(self, row: tuple) -> MemoryRecord:
        """Convert a database row to a MemoryRecord."""
        embedding = None
        if row[8]:  # embedding blob
            import struct
            embedding = list(struct.unpack(f"{len(row[8]) // 4}f", row[8]))
        
        return MemoryRecord(
            id=row[0],
            memory_type=MemoryType.EPISODIC,
            content=row[1],
            embedding=embedding,
            importance=row[2],
            access_count=row[3],
            last_accessed=datetime.fromisoformat(row[4]),
            created_at=datetime.fromisoformat(row[5]),
            expires_at=datetime.fromisoformat(row[6]) if row[6] else None,
            metadata=json.loads(row[7]) if row[7] else {},
        )

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
