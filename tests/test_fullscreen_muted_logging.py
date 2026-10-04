"""The full-screen app must not let stray log lines print over the composer."""

from __future__ import annotations

import logging

from velune.cli.fullscreen import _muted_console_logging


def test_stream_handlers_are_muted_then_restored():
    root = logging.getLogger()
    handler = logging.StreamHandler()
    handler.setLevel(logging.WARNING)
    root.addHandler(handler)
    try:
        with _muted_console_logging():
            assert handler.level > logging.CRITICAL
        assert handler.level == logging.WARNING
    finally:
        root.removeHandler(handler)


def test_file_handlers_are_left_alone(tmp_path):
    root = logging.getLogger()
    handler = logging.FileHandler(tmp_path / "x.log")
    handler.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        with _muted_console_logging():
            assert handler.level == logging.INFO
    finally:
        root.removeHandler(handler)
        handler.close()
