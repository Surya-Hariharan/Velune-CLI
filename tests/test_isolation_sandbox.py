"""The suite never writes to the developer's real Velune state.

``tests/conftest.py`` redirects HOME / app-data before any test module imports
``velune``. These tests pin that: every persistent location resolves inside the
sandbox, and writing through the real code paths lands there.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    bool(os.environ.get("VELUNE_TEST_NO_SANDBOX")), reason="sandbox disabled"
)

# Module-level path constants computed from Path.home() at import time.
HOME_CONSTANTS = [
    ("velune.providers.discovery.gpu", "_DISK_CACHE_PATH"),
    ("velune.cli.workspaces", "DEFAULT_REGISTRY_PATH"),
    ("velune.cli.sessions", "DEFAULT_SESSIONS_DIR"),
    ("velune.cli.session_manager", "SESSIONS_DIR"),
    ("velune.cli.onboarding.logic", "_PROGRESS_FILE"),
    ("velune.cli.model_prefs", "DEFAULT_PREFS_PATH"),
    ("velune.cli.crash_reporter", "CRASH_REPORT_DIR"),
    ("velune.models.registry_cache", "DEFAULT_CACHE_PATH"),
    ("velune.models.local_models", "DEFAULT_PATH"),
    ("velune.providers.ollama_locations", "DEFAULT_LOCATIONS_PATH"),
    ("velune.cli.commands.file_commands", "_USER_COMMANDS_DIR"),
    ("velune.daemon.transport", "DAEMON_SOCKET_PATH"),
    ("velune.daemon.transport", "DAEMON_PID_FILE"),
]


def _inside(path: Path, root: Path) -> bool:
    return root in Path(path).resolve().parents or Path(path).resolve() == root


def test_sandbox_exists_and_is_not_the_real_home(sandbox_root):
    assert sandbox_root is not None and sandbox_root.is_dir()
    assert _inside(Path.home(), sandbox_root)


@pytest.mark.parametrize(("module", "name"), HOME_CONSTANTS)
def test_home_derived_constants_resolve_inside_the_sandbox(sandbox_root, module, name):
    path = getattr(importlib.import_module(module), name)
    assert _inside(path, sandbox_root), f"{module}.{name} -> {path}"


def test_app_data_root_and_storage_dirs_resolve_inside_the_sandbox(sandbox_root, tmp_path):
    from velune.core.paths import app_data_root, cognitive_db_path, workspace_storage_dir

    assert _inside(app_data_root(), sandbox_root)
    assert _inside(workspace_storage_dir(tmp_path), sandbox_root)
    assert _inside(cognitive_db_path(tmp_path), sandbox_root)


def test_workspace_storage_and_migration_marker_land_in_the_sandbox(sandbox_root, tmp_path):
    from velune.core.paths import app_data_root, migrate_legacy_storage, workspace_storage_dir

    assert _inside(app_data_root(), sandbox_root)  # check BEFORE anything is written
    migrate_legacy_storage(tmp_path)
    target = workspace_storage_dir(tmp_path)
    assert (target / ".migrated_from_workspace").exists()
    assert _inside(target, sandbox_root)


def test_gpu_cache_write_lands_in_the_sandbox(sandbox_root):
    from velune.providers.discovery import gpu

    assert _inside(gpu._DISK_CACHE_PATH, sandbox_root)  # check BEFORE writing
    gpu._write_disk_cache({"has_gpu": False})
    assert gpu._DISK_CACHE_PATH.exists()


def test_cognitive_analytics_default_db_is_in_the_sandbox(sandbox_root):
    from velune.core.paths import app_data_root
    from velune.telemetry.cognition import CognitivePerformanceAnalytics

    assert _inside(app_data_root(), sandbox_root)  # check BEFORE the db is created
    analytics = CognitivePerformanceAnalytics()
    analytics.record_injection_attempt("scan_text", "isolation-test")
    assert _inside(Path(analytics.db_path), sandbox_root)


def test_the_os_keyring_is_an_in_memory_backend():
    keyring = pytest.importorskip("keyring")
    backend = keyring.get_keyring()
    assert type(backend).__name__ == "_MemoryKeyring"


# ── Sandbox creation and cleanup ─────────────────────────────────────────────


def test_sandbox_root_avoids_appdata_in_its_path(sandbox_root):
    # A root under ...\AppData\Local\Temp would make every home-derived path look like an
    # %LOCALAPPDATA% path to tests that tell the two apart.
    assert not any(part.lower() == "appdata" for part in sandbox_root.parts[:-2]), sandbox_root


def test_cleanup_removes_a_sandbox_after_the_usage_tracker_wrote_to_sqlite(tmp_path):
    from tests import _sandbox
    from velune.telemetry.usage_tracker import SessionUsageTracker

    root, base = _sandbox.create_sandbox(tmp_path)
    tracker = SessionUsageTracker(db_path=root / "home" / ".velune" / "telemetry" / "usage.db")
    tracker.record_completion("s1", "m", 10, 5, provider_id="p")
    assert tracker.db_path.exists()

    assert _sandbox.remove_sandbox(root, base) == []
    assert not root.exists()
    assert tmp_path.is_dir()  # only the sandbox went, not the base


def test_cleanup_drops_the_global_tracker_so_its_file_is_not_left_locked(tmp_path):
    from tests import _sandbox
    from velune.telemetry import usage_tracker

    root, base = _sandbox.create_sandbox(tmp_path)
    saved = usage_tracker._tracker
    try:
        usage_tracker._tracker = usage_tracker.SessionUsageTracker(db_path=root / "usage.db")
        usage_tracker._tracker.record_completion("s1", "m", 1, 1)
        assert _sandbox.remove_sandbox(root, base) == []
        assert usage_tracker._tracker is None
    finally:
        usage_tracker._tracker = saved


@pytest.mark.skipif(os.name != "nt", reason="POSIX lets an open file be unlinked")
def test_cleanup_reports_the_exact_path_when_a_file_cannot_be_removed(tmp_path):
    from tests import _sandbox

    root, base = _sandbox.create_sandbox(tmp_path)
    held = root / "held.db"
    held.write_bytes(b"x")
    with held.open("rb"):
        leftovers = _sandbox.remove_sandbox(root, base, attempts=2, delay=0.01)
    assert any(path == str(held) for path, _ in leftovers), leftovers
    # Ownership was verified before the first pass; the survivor is under pytest's tmp_path.
    import shutil

    shutil.rmtree(root)
    assert not root.exists()


def test_cleanup_refuses_anything_that_is_not_this_sessions_sandbox(tmp_path):
    from tests import _sandbox

    root, base = _sandbox.create_sandbox(tmp_path)
    try:
        wrong_name = tmp_path / "not-a-sandbox"
        wrong_name.mkdir()
        (wrong_name / "keep.txt").write_text("user data")
        with pytest.raises(RuntimeError, match="prefix"):
            _sandbox.remove_sandbox(wrong_name, tmp_path.resolve())

        stranger = tmp_path / f"{_sandbox.SANDBOX_PREFIX}stranger"
        stranger.mkdir()
        (stranger / "keep.txt").write_text("user data")
        with pytest.raises(RuntimeError, match="marker"):
            _sandbox.remove_sandbox(stranger, tmp_path.resolve())

        (root / _sandbox.MARKER_NAME).write_text("1:other\n")
        with pytest.raises(RuntimeError, match="another process"):
            _sandbox.remove_sandbox(root, base)

        with pytest.raises(RuntimeError, match="parent"):
            _sandbox.remove_sandbox(root, tmp_path.parent)
        assert (wrong_name / "keep.txt").exists() and (stranger / "keep.txt").exists()
        assert root.exists()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
