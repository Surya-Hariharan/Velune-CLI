"""Tests for ErrorLoopDetector."""

from __future__ import annotations

import time

import pytest

from velune.core.loop_detector import ErrorLoopDetector, LoopSignal


class TestErrorLoopDetector:
    def setup_method(self):
        self.detector = ErrorLoopDetector()

    def _exc(self, msg: str = "boom") -> Exception:
        return RuntimeError(msg)

    def test_single_occurrence_not_looping(self):
        sig = self.detector.record(self._exc())
        assert sig.occurrences == 1
        assert sig.is_looping is False

    def test_two_occurrences_not_looping(self):
        exc = self._exc()
        self.detector.record(exc)
        sig = self.detector.record(exc)
        assert sig.occurrences == 2
        assert sig.is_looping is False

    def test_three_occurrences_triggers_loop(self):
        exc = self._exc()
        self.detector.record(exc)
        self.detector.record(exc)
        sig = self.detector.record(exc)
        assert sig.occurrences == 3
        assert sig.is_looping is True

    def test_clear_resets_state(self):
        exc = self._exc()
        self.detector.record(exc)
        self.detector.record(exc)
        sig = self.detector.record(exc)
        assert sig.is_looping is True

        self.detector.clear(sig.fingerprint)
        # After clearing, a new recording starts fresh
        sig2 = self.detector.record(exc)
        assert sig2.occurrences == 1
        assert sig2.is_looping is False

    def test_different_errors_tracked_independently(self):
        exc_a = RuntimeError("error A")
        exc_b = ValueError("error B")
        for _ in range(3):
            self.detector.record(exc_a)
        sig_b = self.detector.record(exc_b)
        assert sig_b.is_looping is False
        assert sig_b.occurrences == 1

    def test_is_looping_without_recording(self):
        exc = self._exc()
        # Not yet recorded → not looping
        assert self.detector.is_looping(exc) is False
        for _ in range(3):
            self.detector.record(exc)
        assert self.detector.is_looping(exc) is True

    def test_sliding_window_evicts_old_entries(self, monkeypatch):
        exc = self._exc()
        times = [0.0]

        def fake_monotonic():
            return times[0]

        monkeypatch.setattr(time, "monotonic", fake_monotonic)

        # Record at t=0, t=1, t=2
        for delta in (0, 1, 2):
            times[0] = float(delta)
            self.detector.record(exc)

        # Move time past the 5-minute window
        times[0] = 305.0
        sig = self.detector.record(exc)
        # Old entries evicted; only the new one at 305 counts
        assert sig.occurrences == 1
        assert sig.is_looping is False

    def test_loopsignal_fields(self):
        exc = ValueError("test error")
        sig = self.detector.record(exc)
        assert sig.error_type == "ValueError"
        assert "test error" in sig.error_preview
        assert isinstance(sig.fingerprint, str)
        assert len(sig.fingerprint) == 16

    def test_clear_all(self):
        for _ in range(3):
            self.detector.record(self._exc())
        self.detector.clear_all()
        sig = self.detector.record(self._exc())
        assert sig.occurrences == 1
