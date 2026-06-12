"""Tests for tree-sitter AST parser and symbol extraction."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from velune.repository.ast_parser import ASTParser, Symbol, SymbolKind
from velune.repository.rename_journal import RenameJournal
from velune.repository.symbol_registry import SymbolRegistry


PYTHON_SAMPLE = '''"""Sample Python module."""

def validate_token(token: str) -> bool:
    """Check if token is valid."""
    return len(token) > 0


def process_data(data):
    """Process input data."""
    return data


class DataProcessor:
    """Main data processor class."""

    def __init__(self, name: str):
        """Initialize processor."""
        self.name = name

    def process(self, item):
        """Process an item."""
        return item


import os
from typing import List
'''


@pytest.mark.asyncio
async def test_python_symbol_extraction():
    """Test extracting symbols from Python file."""
    parser = ASTParser(["python"])

    with tempfile.TemporaryDirectory() as tmpdir:
        py_file = Path(tmpdir) / "test.py"
        py_file.write_text(PYTHON_SAMPLE)

        result = await parser.parse_file(py_file)

        assert result is not None
        assert result.language == "python"
        assert len(result.symbols) > 0

        # Check for functions
        functions = [s for s in result.symbols if s.kind == SymbolKind.FUNCTION]
        assert len(functions) >= 2

        # Verify function names
        func_names = {f.name for f in functions}
        assert "validate_token" in func_names


@pytest.mark.asyncio
async def test_symbol_registry_upsert_and_retrieve():
    """Test symbol registry storage and retrieval."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "symbols.db"
        registry = SymbolRegistry(db_path)
        await registry.initialize()

        # Create test symbols
        symbols = [
            Symbol(
                id="sym-1",
                name="validate_token",
                kind=SymbolKind.FUNCTION,
                file_path="module.py",
                line_start=1,
                line_end=3,
                docstring="Validate token",
                parameters=["token"],
            ),
            Symbol(
                id="sym-2",
                name="DataProcessor",
                kind=SymbolKind.CLASS,
                file_path="module.py",
                line_start=6,
                line_end=12,
            ),
        ]

        # Store symbols
        await registry.upsert_symbols("module.py", symbols)

        # Retrieve and verify
        retrieved = await registry.get_symbols("module.py")
        assert len(retrieved) == 2
        assert retrieved[0].name == "validate_token"


@pytest.mark.asyncio
async def test_symbol_registry_search():
    """Test symbol search functionality."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "symbols.db"
        registry = SymbolRegistry(db_path)
        await registry.initialize()

        symbols = [
            Symbol(
                id="sym-1",
                name="validate_token",
                kind=SymbolKind.FUNCTION,
                file_path="module.py",
                line_start=1,
                line_end=3,
            ),
            Symbol(
                id="sym-2",
                name="validate_key",
                kind=SymbolKind.FUNCTION,
                file_path="module.py",
                line_start=5,
                line_end=7,
            ),
        ]

        await registry.upsert_symbols("module.py", symbols)

        # Search for "validate*"
        results = await registry.search_symbols("validate%")
        assert len(results) == 2


@pytest.mark.asyncio
async def test_rename_detection():
    """Test detecting renames between symbol versions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "renames.db"
        journal = RenameJournal(db_path)
        await journal.initialize()

        old_symbols = {
            "validate_token": ("sym-1", 1),
            "process_data": ("sym-2", 5),
        }

        new_symbols = {
            "verifyJWT": ("sym-1", 1),
            "process_data": ("sym-2", 5),
        }

        renames = await journal.detect_rename(old_symbols, new_symbols, "module.py")

        assert len(renames) == 1
        assert renames[0].old_name == "validate_token"
        assert renames[0].new_name == "verifyJWT"


@pytest.mark.asyncio
async def test_stable_id_preserved_across_rename():
    """Test that symbol IDs remain stable across renames."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "symbols.db"
        registry = SymbolRegistry(db_path)
        await registry.initialize()

        original = Symbol(
            id="stable-id-123",
            name="validate_token",
            kind=SymbolKind.FUNCTION,
            file_path="module.py",
            line_start=1,
            line_end=3,
        )

        await registry.upsert_symbols("module.py", [original])

        renamed = Symbol(
            id="stable-id-123",
            name="verifyJWT",
            kind=SymbolKind.FUNCTION,
            file_path="module.py",
            line_start=1,
            line_end=3,
        )

        await registry.upsert_symbols("module.py", [renamed])

        retrieved = await registry.get_symbol_by_id("stable-id-123")
        assert retrieved is not None
        assert retrieved.id == "stable-id-123"
        assert retrieved.name == "verifyJWT"
