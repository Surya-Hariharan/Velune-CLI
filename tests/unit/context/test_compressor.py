import pytest
import tempfile
import pathlib
from unittest.mock import MagicMock

from velune.context.extractive import extractive_compress
from velune.context.compressor import ContextCompressor
from velune.telemetry.cognition import CognitivePerformanceAnalytics
from velune.context.window import estimate_tokens

def test_extractive_compress_reduces_size():
    # Generate unique sentences to prevent term-frequency crowding
    sentences = [
        f"This is sentence number {i} explaining database concepts and core architecture configurations."
        for i in range(120)
    ]
    # Set first and last sentence
    sentences[0] = "This is the first sentence of the text and must be preserved."
    sentences[-1] = "The end is near and we must preserve the final statement."
    # Insert some code-like construct sentences
    sentences[10] = "We want to write clean python code class MyDatabaseManager: def __init__(self): pass."
    sentences[50] = "This describes importing dependencies: import os, sys."
    
    long_text = " ".join(sentences)
    original_tokens = estimate_tokens(long_text)
    assert original_tokens > 1500

    
    # Target 600 tokens
    compressed = extractive_compress(long_text, target_tokens=600)
    compressed_tokens = estimate_tokens(compressed)
    
    # Assert size is significantly reduced
    assert compressed_tokens <= 800
    # Assert first and last sentences are preserved
    assert sentences[0] in compressed
    assert sentences[-1] in compressed
    # Assert code boosted sentences are preserved
    assert "class MyDatabaseManager:" in compressed
    # Assert footer was appended
    assert "[COMPRESSED:" in compressed

def test_compressor_falls_back_when_provider_is_none():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        analytics_db = pathlib.Path(tmpdir) / "test_analytics.db"
        analytics = CognitivePerformanceAnalytics(db_path=analytics_db)
        
        compressor = ContextCompressor(analytics=analytics)
        
        # Generate unique sentences
        sentences = [
            f"Statement number {i} describes some internal logging structures and details."
            for i in range(100)
        ]
        sentences[0] = "Preserve this first statement."
        sentences[-1] = "Preserve this last statement."
        
        long_content = " ".join(sentences)
        
        # Call compress with provider=None
        result = await_async(compressor.compress(
            content=long_content,
            provider=None,
            model_id="test-model",
            target_token_budget=150
        ))
        
        assert result is not None
        assert "Preserve this first statement." in result
        assert "Preserve this last statement." in result
        
        # Verify telemetry was recorded in SQLite
        with analytics._get_connection() as conn:
            row = conn.execute("SELECT * FROM compression_metrics").fetchone()
            assert row is not None
            assert row["original_tokens"] > 150
            assert row["compressed_tokens"] < row["original_tokens"]
            assert row["method"] == "extractive"
            assert row["latency_ms"] >= 0

def test_critical_lines_preserved():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmpdir:
        analytics_db = pathlib.Path(tmpdir) / "test_analytics.db"
        analytics = CognitivePerformanceAnalytics(db_path=analytics_db)
        
        compressor = ContextCompressor(analytics=analytics)
        
        # Generate enough regular lines (>150 tokens) to avoid early return (budget-max is 100)
        regular_sentences = [
            f"This is normal descriptive sentence {i} that should be compressed to save space."
            for i in range(50)
        ]
        
        content = (
            "\n".join(regular_sentences) + "\n"
            "[ ] - Critical incomplete task that must be preserved.\n"
            "ERROR: Connection refused during remote execution.\n"
        )
        
        # Call compress with provider=None and budget of 150 tokens
        # critical_tokens will be small, so remaining_budget will be max(100, 150 - critical) -> ~120 tokens.
        # since regular lines have 50 * ~12 tokens = ~600 tokens > 120, it will compress!
        result = await_async(compressor.compress(
            content=content,
            provider=None,
            model_id="test-model",
            target_token_budget=150
        ))
        
        # Verify headers exist
        assert "### CRITICAL SYSTEM GUIDELINES & UNRESOLVED STEPS (PRESERVED) ###" in result
        assert "### COMPRESSED CONTEXT ###" in result
        # Verify critical lines are preserved intact
        assert "[ ] - Critical incomplete task that must be preserved." in result
        assert "ERROR: Connection refused during remote execution." in result

def await_async(coro):
    import asyncio
    return asyncio.run(coro)
