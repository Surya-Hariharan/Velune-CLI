"""Journal tracking symbol renames across the repository."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger("velune.repository.rename_journal")


@dataclass
class RenameRecord:
    """Record of a symbol rename."""
    symbol_id: str
    old_name: str
    new_name: str
    file_path: str
    old_line: int
    new_line: int
    timestamp: datetime


class RenameJournal:
    """Persistent journal of symbol renames."""

    def __init__(self, db_path: Path) -> None:
        """Initialize rename journal.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        """Initialize database schema."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS renames (
                    symbol_id TEXT NOT NULL,
                    old_name TEXT NOT NULL,
                    new_name TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    old_line INTEGER NOT NULL,
                    new_line INTEGER NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (symbol_id, timestamp)
                );

                CREATE INDEX IF NOT EXISTS idx_renames_symbol ON renames(symbol_id);
                CREATE INDEX IF NOT EXISTS idx_renames_file ON renames(file_path);
                CREATE INDEX IF NOT EXISTS idx_renames_old_name ON renames(old_name);
                CREATE INDEX IF NOT EXISTS idx_renames_timestamp ON renames(timestamp);
            """)
            await db.commit()

    async def record_rename(
        self,
        symbol_id: str,
        old_name: str,
        new_name: str,
        file_path: str,
        old_line: int,
        new_line: int,
    ) -> None:
        """Record a symbol rename.

        Args:
            symbol_id: Stable symbol ID
            old_name: Previous symbol name
            new_name: New symbol name
            file_path: File path where rename occurred
            old_line: Previous line number
            new_line: New line number
        """
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.execute(
                """
                INSERT INTO renames (symbol_id, old_name, new_name, file_path, old_line, new_line)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (symbol_id, old_name, new_name, file_path, old_line, new_line),
            )
            await db.commit()

        logger.debug(f"Recorded rename: {old_name} → {new_name} (id={symbol_id})")

    async def get_renames_for_symbol(self, symbol_id: str) -> list[RenameRecord]:
        """Get all rename records for a symbol.

        Args:
            symbol_id: Symbol to get renames for

        Returns:
            List of rename records in chronological order
        """
        records: list[RenameRecord] = []
        async with aiosqlite.connect(str(self.db_path)) as db:
            async for row in db.execute(
                """
                SELECT symbol_id, old_name, new_name, file_path, old_line, new_line, timestamp
                FROM renames WHERE symbol_id = ? ORDER BY timestamp ASC
                """,
                (symbol_id,),
            ):
                record = RenameRecord(
                    symbol_id=row[0],
                    old_name=row[1],
                    new_name=row[2],
                    file_path=row[3],
                    old_line=row[4],
                    new_line=row[5],
                    timestamp=datetime.fromisoformat(row[6]),
                )
                records.append(record)

        return records

    async def resolve_name(self, old_name: str, file_path: str) -> str | None:
        """Resolve an old symbol name to its current name.

        Searches the most recent rename record for the given name and file.

        Args:
            old_name: Historical symbol name
            file_path: File path

        Returns:
            Current name if rename exists, None otherwise
        """
        async with aiosqlite.connect(str(self.db_path)) as db:
            row = await db.execute_fetchone(
                """
                SELECT new_name FROM renames
                WHERE old_name = ? AND file_path = ?
                ORDER BY timestamp DESC LIMIT 1
                """,
                (old_name, file_path),
            )

        if row:
            return row[0]

        # Check if there's a chain of renames
        # (i.e., A→B→C, resolve A to C)
        current_name = old_name
        depth = 0
        max_depth = 10

        while depth < max_depth:
            next_name = await self.resolve_name(current_name, file_path)
            if not next_name or next_name == current_name:
                break
            current_name = next_name
            depth += 1

        return current_name if current_name != old_name else None

    async def get_renames_in_file(self, file_path: str) -> list[RenameRecord]:
        """Get all renames in a specific file.

        Args:
            file_path: File path to get renames for

        Returns:
            List of rename records
        """
        records: list[RenameRecord] = []
        async with aiosqlite.connect(str(self.db_path)) as db:
            async for row in db.execute(
                """
                SELECT symbol_id, old_name, new_name, file_path, old_line, new_line, timestamp
                FROM renames WHERE file_path = ? ORDER BY timestamp ASC
                """,
                (file_path,),
            ):
                record = RenameRecord(
                    symbol_id=row[0],
                    old_name=row[1],
                    new_name=row[2],
                    file_path=row[3],
                    old_line=row[4],
                    new_line=row[5],
                    timestamp=datetime.fromisoformat(row[6]),
                )
                records.append(record)

        return records

    async def detect_rename(
        self,
        old_symbols: dict[str, tuple[str, int]],  # name -> (id, line)
        new_symbols: dict[str, tuple[str, int]],  # name -> (id, line)
        file_path: str,
    ) -> list[RenameRecord]:
        """Detect renames by comparing old and new symbol lists.

        Heuristic: symbols at approximately the same line in the same file
        with the same ID are considered the same symbol, even if the name changed.

        Args:
            old_symbols: Map of name -> (symbol_id, line) from previous parse
            new_symbols: Map of name -> (symbol_id, line) from current parse
            file_path: File path being analyzed

        Returns:
            List of detected renames
        """
        detected_renames: list[RenameRecord] = []

        # Build reverse map: id -> (name, line)
        old_by_id = {sym_id: (name, line) for name, (sym_id, line) in old_symbols.items()}
        new_by_id = {sym_id: (name, line) for name, (sym_id, line) in new_symbols.items()}

        # Find renamed symbols: same ID, different name
        for symbol_id, (new_name, new_line) in new_by_id.items():
            if symbol_id in old_by_id:
                old_name, old_line = old_by_id[symbol_id]
                if old_name != new_name:
                    # Rename detected
                    await self.record_rename(
                        symbol_id=symbol_id,
                        old_name=old_name,
                        new_name=new_name,
                        file_path=file_path,
                        old_line=old_line,
                        new_line=new_line,
                    )

                    record = RenameRecord(
                        symbol_id=symbol_id,
                        old_name=old_name,
                        new_name=new_name,
                        file_path=file_path,
                        old_line=old_line,
                        new_line=new_line,
                        timestamp=datetime.now(),
                    )
                    detected_renames.append(record)

        return detected_renames

    async def get_all_renames(self) -> list[RenameRecord]:
        """Get all rename records in the journal.

        Returns:
            List of all rename records
        """
        records: list[RenameRecord] = []
        async with aiosqlite.connect(str(self.db_path)) as db:
            async for row in db.execute(
                """
                SELECT symbol_id, old_name, new_name, file_path, old_line, new_line, timestamp
                FROM renames ORDER BY timestamp ASC
                """
            ):
                record = RenameRecord(
                    symbol_id=row[0],
                    old_name=row[1],
                    new_name=row[2],
                    file_path=row[3],
                    old_line=row[4],
                    new_line=row[5],
                    timestamp=datetime.fromisoformat(row[6]),
                )
                records.append(record)

        return records

    async def clear_renames_for_file(self, file_path: str) -> None:
        """Clear all rename records for a file.

        Args:
            file_path: File path to clear renames for
        """
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.execute("DELETE FROM renames WHERE file_path = ?", (file_path,))
            await db.commit()
