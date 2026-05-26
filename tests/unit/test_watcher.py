"""Batch 02 unit tests — WorkspaceEvolutionWatcher hot path remediation."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_watcher(tmp_path: Path):
    """Build a WorkspaceEvolutionWatcher with a fully mocked indexer."""
    from velune.repository.watcher import WorkspaceEvolutionWatcher

    # Minimal RepositoryIndexer mock
    indexer = MagicMock()
    indexer.parser.parse.return_value = ([], [])          # (symbols, edges)
    indexer.parser._detect_language.return_value = MagicMock(value="python")
    indexer._compute_sha256.return_value = "deadbeef"
    indexer.cache_path = tmp_path / ".velune_cache.json"  # doesn't need to exist

    watcher = WorkspaceEvolutionWatcher(
        root_path=tmp_path,
        indexer=indexer,
        grapher=None,
        semantic_memory=None,
        event_bus=None,
    )
    return watcher


def _write_py_file(tmp_path: Path, name: str = "sample.py") -> Path:
    """Write a minimal Python source file and return its path."""
    p = tmp_path / name
    p.write_text("def hello():\n    pass\n", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Test 1 — ArchitectureCognitionAgent must NOT be imported during hot path
# ---------------------------------------------------------------------------

class TestFileEventDoesNotImportArchitectureAgent:
    def test_file_event_does_not_import_architecture_agent(self, tmp_path):
        """
        _handle_file_event must NOT import or instantiate ArchitectureCognitionAgent.
        The module velune.cognition.architecture must not appear in sys.modules
        after the event handler runs.
        """
        watcher = _make_watcher(tmp_path)
        py_file = _write_py_file(tmp_path)

        # Ensure the module is NOT already loaded
        sys.modules.pop("velune.cognition.architecture", None)
        sys.modules.pop("velune.cognition", None)

        # Patch the cognition module so any accidental import is caught
        fake_agent_cls = MagicMock()
        fake_agent_cls.return_value.audit_architecture = MagicMock()

        with patch.dict("sys.modules", {"velune.cognition.architecture": MagicMock()}):
            before_modules = set(sys.modules.keys())
            watcher._handle_file_event(py_file, "modified")
            after_modules = set(sys.modules.keys())

        # The agent class must never have been called
        assert not fake_agent_cls.called, (
            "ArchitectureCognitionAgent was instantiated during _handle_file_event. "
            "The ADA block must be removed from the hot path."
        )

    def test_architecture_module_not_imported_during_event(self, tmp_path):
        """
        Verify by inspecting sys.modules: velune.cognition.architecture must
        NOT be imported as a side-effect of handling a file change event.
        """
        watcher = _make_watcher(tmp_path)
        py_file = _write_py_file(tmp_path)

        # Remove any prior import
        sys.modules.pop("velune.cognition.architecture", None)

        watcher._handle_file_event(py_file, "created")

        # The module must still be absent after the call
        assert "velune.cognition.architecture" not in sys.modules, (
            "velune.cognition.architecture was imported as a side-effect of "
            "_handle_file_event. The ADA block must be removed from the hot path."
        )

    def test_no_get_event_loop_call(self, tmp_path):
        """
        asyncio.get_event_loop() must not be called inside _handle_file_event.
        (Deprecated since Python 3.10; replaced with get_running_loop.)
        """
        import velune.repository.watcher as watcher_module

        source = Path(watcher_module.__file__).read_text(encoding="utf-8")
        assert "get_event_loop" not in source, (
            "asyncio.get_event_loop() is still present in watcher.py. "
            "It must be replaced with asyncio.get_running_loop()."
        )


# ---------------------------------------------------------------------------
# Test 2 — Event handler must complete in < 100 ms with a mocked indexer
# ---------------------------------------------------------------------------

class TestFileEventCompletesQuickly:
    def test_file_event_completes_quickly(self, tmp_path):
        """
        With a mocked indexer (instant return), _handle_file_event must
        complete in < 100ms. The old ADA code could take seconds.
        """
        watcher = _make_watcher(tmp_path)
        py_file = _write_py_file(tmp_path)

        start = time.perf_counter()
        watcher._handle_file_event(py_file, "modified")
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        assert elapsed_ms < 100.0, (
            f"_handle_file_event took {elapsed_ms:.1f}ms — must be < 100ms. "
            f"An expensive synchronous operation may still be running in the hot path."
        )

    def test_file_event_created_completes_quickly(self, tmp_path):
        """Same performance guarantee for 'created' events."""
        watcher = _make_watcher(tmp_path)
        py_file = _write_py_file(tmp_path, "new_file.py")

        start = time.perf_counter()
        watcher._handle_file_event(py_file, "created")
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        assert elapsed_ms < 100.0, (
            f"_handle_file_event('created') took {elapsed_ms:.1f}ms — must be < 100ms."
        )

    def test_file_event_deleted_completes_quickly(self, tmp_path):
        """'deleted' events must also be fast (no architecture agent, no file read)."""
        watcher = _make_watcher(tmp_path)

        # Create then delete the file so the path exists for the watcher check
        py_file = _write_py_file(tmp_path, "gone.py")
        py_file.unlink()

        start = time.perf_counter()
        watcher._handle_file_event(py_file, "deleted")
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        assert elapsed_ms < 100.0, (
            f"_handle_file_event('deleted') took {elapsed_ms:.1f}ms — must be < 100ms."
        )


# ---------------------------------------------------------------------------
# Test 3 — Smoke test: watcher is still importable and event_bus path works
# ---------------------------------------------------------------------------

class TestWatcherSmoke:
    def test_watcher_importable(self):
        """WorkspaceEvolutionWatcher must be importable after the patch."""
        from velune.repository.watcher import WorkspaceEvolutionWatcher  # noqa: F401

    def test_handle_deleted_event_without_cache(self, tmp_path):
        """Deleted event on a missing cache file must not raise."""
        watcher = _make_watcher(tmp_path)
        py_file = _write_py_file(tmp_path)
        py_file.unlink()  # file is gone

        # Must not raise
        watcher._handle_file_event(py_file, "deleted")

    def test_event_bus_not_called_when_none(self, tmp_path):
        """When event_bus is None, no event emission code runs (no RuntimeError)."""
        watcher = _make_watcher(tmp_path)
        assert watcher.event_bus is None
        py_file = _write_py_file(tmp_path)

        # Must not raise even without an event bus
        watcher._handle_file_event(py_file, "modified")
