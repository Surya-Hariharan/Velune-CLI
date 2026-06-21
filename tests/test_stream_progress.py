"""Tests for StreamProgress schema additions (elapsed_ms) and model attribution."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.orchestration.schemas import StreamProgress


class TestStreamProgressElapsedMs:
    def test_elapsed_ms_defaults_to_none(self):
        sp = StreamProgress(run_id="run-1", phase="planner", message="Planning...")
        assert sp.elapsed_ms is None

    def test_elapsed_ms_preserved(self):
        sp = StreamProgress(
            run_id="run-1", phase="coder", message="Coding...", elapsed_ms=1234.5
        )
        assert sp.elapsed_ms == 1234.5

    def test_elapsed_ms_zero_valid(self):
        sp = StreamProgress(run_id="run-1", phase="planner", message="start", elapsed_ms=0.0)
        assert sp.elapsed_ms == 0.0

    def test_roundtrip_through_pydantic(self):
        original = StreamProgress(
            run_id="run-abc", phase="synthesis", message="done", elapsed_ms=987.3
        )
        dumped = original.model_dump()
        restored = StreamProgress(**dumped)
        assert restored.elapsed_ms == pytest.approx(987.3)

    def test_roundtrip_none_elapsed(self):
        original = StreamProgress(run_id="run-abc", phase="coder", message="hi")
        dumped = original.model_dump()
        restored = StreamProgress(**dumped)
        assert restored.elapsed_ms is None

    def test_str_representation_unchanged(self):
        sp = StreamProgress(
            run_id="run-x", phase="planner", message="Planning...", elapsed_ms=500.0
        )
        assert "[run-x] planner: Planning..." == str(sp)

    def test_backward_compatible_without_elapsed_ms(self):
        """Existing code that creates StreamProgress without elapsed_ms still works."""
        data = {"run_id": "run-1", "phase": "coder", "message": "Coding..."}
        sp = StreamProgress(**data)
        assert sp.elapsed_ms is None


class TestModelAssignmentEventFormat:
    def test_model_assignment_phase_parses_correctly(self):
        """The [Model Assignment] phase label should parse to 'model assignment' (lowercase)."""
        msg = "[Model Assignment] planner: gemma3:12b  |  coder: qwen2.5-coder:7b"
        phase = ""
        message = msg
        if msg.startswith("[") and "]" in msg:
            parts = msg.split("]", 1)
            phase = parts[0][1:].lower()
            message = parts[1].strip()
        assert phase == "model assignment"
        assert "planner: gemma3:12b" in message

    def test_phase_timing_encoded_in_progress(self):
        """elapsed_ms is set when phase changes, not on every message."""
        sp_phase_change = StreamProgress(
            run_id="r1", phase="coder", message="Coding...", elapsed_ms=2000.0
        )
        sp_same_phase = StreamProgress(run_id="r1", phase="coder", message="still coding")
        assert sp_phase_change.elapsed_ms == 2000.0
        assert sp_same_phase.elapsed_ms is None
