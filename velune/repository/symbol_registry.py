"""SQLite-backed symbol registry for tracking code symbols."""

from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

from velune.repository.ast_parser import Symbol, SymbolKind

logger = logging.getLogger("velune.repository.symbol_registry")


class SymbolRegistry:
    """Persistent symbol registry using SQLite."""

    def __init__(self, db_path: Path) -> None:
        """Initialize symbol registry.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    async def initialize(self) -> None:
        """Initialize database schema."""
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS symbols (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    docstring TEXT,
                    parameters TEXT,  -- JSON array
                    return_type TEXT,
                    is_exported BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(file_path, name, line_start)
                );

                CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);
                CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
                CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
            """)
            await db.commit()

    async def upsert_symbols(self, file_path: str, symbols: list[Symbol]) -> None:
        """Store or update symbols from a file.

        Updates existing symbols by (file_path, name, line_start).
        Preserves stable IDs when possible.

        Args:
            file_path: Relative path to file
            symbols: List of symbols to store
        """
        import json

        async with aiosqlite.connect(str(self.db_path)) as db:
            db.row_factory = aiosqlite.Row
            for symbol in symbols:
                params = json.dumps(symbol.parameters) if symbol.parameters else None

                # 1. Check if ID exists
                async with db.execute("SELECT 1 FROM symbols WHERE id = ?", (symbol.id,)) as cursor:
                    exists_by_id = await cursor.fetchone()

                if exists_by_id:
                    # Update by ID (rename or modification)
                    await db.execute(
                        """
                        UPDATE symbols SET
                            name = ?,
                            kind = ?,
                            file_path = ?,
                            line_start = ?,
                            line_end = ?,
                            docstring = ?,
                            parameters = ?,
                            return_type = ?,
                            is_exported = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (
                            symbol.name,
                            symbol.kind.value,
                            file_path,
                            symbol.line_start,
                            symbol.line_end,
                            symbol.docstring,
                            params,
                            symbol.return_type,
                            1 if symbol.is_exported else 0,
                            symbol.id,
                        ),
                    )
                else:
                    # 2. Check if file_path, name, line_start exists
                    async with db.execute(
                        "SELECT id FROM symbols WHERE file_path = ? AND name = ? AND line_start = ?",
                        (file_path, symbol.name, symbol.line_start),
                    ) as cursor:
                        existing_row = await cursor.fetchone()

                    if existing_row:
                        # Update by file_path/name/line_start, preserving existing ID
                        existing_id = existing_row[0]
                        await db.execute(
                            """
                            UPDATE symbols SET
                                kind = ?,
                                line_end = ?,
                                docstring = ?,
                                parameters = ?,
                                return_type = ?,
                                is_exported = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ?
                            """,
                            (
                                symbol.kind.value,
                                symbol.line_end,
                                symbol.docstring,
                                params,
                                symbol.return_type,
                                1 if symbol.is_exported else 0,
                                existing_id,
                            ),
                        )
                    else:
                        # 3. Insert new row
                        await db.execute(
                            """
                            INSERT INTO symbols (
                                id, name, kind, file_path, line_start, line_end,
                                docstring, parameters, return_type, is_exported
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                symbol.id,
                                symbol.name,
                                symbol.kind.value,
                                file_path,
                                symbol.line_start,
                                symbol.line_end,
                                symbol.docstring,
                                params,
                                symbol.return_type,
                                1 if symbol.is_exported else 0,
                            ),
                        )

            await db.commit()

    async def get_symbols(self, file_path: str) -> list[Symbol]:
        """Retrieve all symbols from a file.

        Args:
            file_path: Relative path to file

        Returns:
            List of symbols in the file
        """
        import json

        symbols: list[Symbol] = []
        async with aiosqlite.connect(str(self.db_path)) as db:
            async with db.execute(
                "SELECT id, name, kind, file_path, line_start, line_end, "
                "docstring, parameters, return_type, is_exported FROM symbols "
                "WHERE file_path = ? ORDER BY line_start",
                (file_path,),
            ) as cursor:
                async for row in cursor:
                    params = json.loads(row[7]) if row[7] else []
                    symbol = Symbol(
                        id=row[0],
                        name=row[1],
                        kind=SymbolKind(row[2]),
                        file_path=row[3],
                        line_start=row[4],
                        line_end=row[5],
                        docstring=row[6],
                        parameters=params,
                        return_type=row[8],
                        is_exported=bool(row[9]),
                    )
                    symbols.append(symbol)

        return symbols

    async def search_symbols(self, name_pattern: str) -> list[Symbol]:
        """Search for symbols by name pattern (LIKE search).

        Args:
            name_pattern: SQL LIKE pattern (e.g., 'validate%')

        Returns:
            List of matching symbols
        """
        import json

        symbols: list[Symbol] = []
        async with aiosqlite.connect(str(self.db_path)) as db:
            async with db.execute(
                "SELECT id, name, kind, file_path, line_start, line_end, "
                "docstring, parameters, return_type, is_exported FROM symbols "
                "WHERE name LIKE ? ORDER BY file_path, line_start",
                (name_pattern,),
            ) as cursor:
                async for row in cursor:
                    params = json.loads(row[7]) if row[7] else []
                    symbol = Symbol(
                        id=row[0],
                        name=row[1],
                        kind=SymbolKind(row[2]),
                        file_path=row[3],
                        line_start=row[4],
                        line_end=row[5],
                        docstring=row[6],
                        parameters=params,
                        return_type=row[8],
                        is_exported=bool(row[9]),
                    )
                    symbols.append(symbol)

        return symbols

    async def get_symbol_by_id(self, symbol_id: str) -> Symbol | None:
        """Retrieve a symbol by its stable ID.

        Args:
            symbol_id: Unique symbol ID

        Returns:
            Symbol if found, None otherwise
        """
        import json

        async with aiosqlite.connect(str(self.db_path)) as db:
            async with db.execute(
                "SELECT id, name, kind, file_path, line_start, line_end, "
                "docstring, parameters, return_type, is_exported FROM symbols "
                "WHERE id = ?",
                (symbol_id,),
            ) as cursor:
                row = await cursor.fetchone()

        if not row:
            return None

        params = json.loads(row[7]) if row[7] else []
        return Symbol(
            id=row[0],
            name=row[1],
            kind=SymbolKind(row[2]),
            file_path=row[3],
            line_start=row[4],
            line_end=row[5],
            docstring=row[6],
            parameters=params,
            return_type=row[8],
            is_exported=bool(row[9]),
        )

    async def remove_file_symbols(self, file_path: str) -> None:
        """Remove all symbols from a file.

        Called when a file is deleted or removed from tracking.

        Args:
            file_path: Relative path to file
        """
        async with aiosqlite.connect(str(self.db_path)) as db:
            await db.execute("DELETE FROM symbols WHERE file_path = ?", (file_path,))
            await db.commit()

    async def get_all_symbols(self) -> list[Symbol]:
        """Retrieve all symbols in registry.

        Returns:
            List of all symbols
        """
        import json

        symbols: list[Symbol] = []
        async with aiosqlite.connect(str(self.db_path)) as db:
            async with db.execute(
                "SELECT id, name, kind, file_path, line_start, line_end, "
                "docstring, parameters, return_type, is_exported FROM symbols "
                "ORDER BY file_path, line_start"
            ) as cursor:
                async for row in cursor:
                    params = json.loads(row[7]) if row[7] else []
                    symbol = Symbol(
                        id=row[0],
                        name=row[1],
                        kind=SymbolKind(row[2]),
                        file_path=row[3],
                        line_start=row[4],
                        line_end=row[5],
                        docstring=row[6],
                        parameters=params,
                        return_type=row[8],
                        is_exported=bool(row[9]),
                    )
                    symbols.append(symbol)

        return symbols

    async def get_symbols_by_kind(self, kind: SymbolKind) -> list[Symbol]:
        """Retrieve all symbols of a specific kind.

        Args:
            kind: Symbol kind to filter by

        Returns:
            List of symbols of specified kind
        """
        import json

        symbols: list[Symbol] = []
        async with aiosqlite.connect(str(self.db_path)) as db:
            async with db.execute(
                "SELECT id, name, kind, file_path, line_start, line_end, "
                "docstring, parameters, return_type, is_exported FROM symbols "
                "WHERE kind = ? ORDER BY file_path, line_start",
                (kind.value,),
            ) as cursor:
                async for row in cursor:
                    params = json.loads(row[7]) if row[7] else []
                    symbol = Symbol(
                        id=row[0],
                        name=row[1],
                        kind=SymbolKind(row[2]),
                        file_path=row[3],
                        line_start=row[4],
                        line_end=row[5],
                        docstring=row[6],
                        parameters=params,
                        return_type=row[8],
                        is_exported=bool(row[9]),
                    )
                    symbols.append(symbol)

        return symbols

    async def count_symbols(self) -> int:
        """Count total symbols in registry.

        Returns:
            Total number of symbols
        """
        async with aiosqlite.connect(str(self.db_path)) as db:
            async with db.execute("SELECT COUNT(*) FROM symbols") as cursor:
                row = await cursor.fetchone()

        return row[0] if row else 0
