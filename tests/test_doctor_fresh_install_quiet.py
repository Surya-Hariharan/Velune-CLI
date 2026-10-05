"""A fresh, lean install must not print ERROR lines or a "FAIL" banner.

Regressions seen running `velune doctor` on a clean `pip install velune-cli`:
* `LanceDB startup failed … No module named 'lancedb'` logged at ERROR — the
  optional [rag] extra being absent is a supported configuration;
* `Failed to list recent sessions: no such table: sessions` logged at ERROR —
  the memory-health check opened (creating) an empty, schema-less store for
  whatever folder doctor ran in;
* the summary banner said FAIL because an *optional* integration failed,
  contradicting the "Core installation is healthy" verdict below it.
"""

from __future__ import annotations

import io
import logging
import sys

from rich.console import Console

from velune.cli.commands import doctor
from velune.kernel.entrypoint import run_async
from velune.memory.storage.lancedb_store import LanceDBStore


def test_missing_rag_extra_is_not_logged_as_an_error(tmp_path, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "lancedb", None)  # import → ImportError
    store = LanceDBStore(tmp_path / "store")
    with caplog.at_level(logging.INFO, logger="velune.memory.storage.lancedb_store"):
        run_async(store.startup())
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "optional [rag] extra not installed" in caplog.text


def test_memory_check_in_a_fresh_folder_is_read_only_and_quiet(tmp_path, monkeypatch, caplog):
    data = tmp_path / "data"
    project = tmp_path / "never-used-project"
    project.mkdir()
    monkeypatch.setenv("VELUNE_DATA_HOME", str(data))
    monkeypatch.chdir(project)

    with caplog.at_level(logging.WARNING):
        result = doctor._check_memory_health()

    assert result["status"] == "ok"
    assert "created on the first" in result["message"]
    assert not caplog.records, [r.getMessage() for r in caplog.records]
    assert not (data / "workspaces").exists(), "doctor created storage for an unused folder"
    assert list(project.iterdir()) == []


def test_banner_does_not_say_fail_when_only_optional_checks_fail():
    results = [
        {"name": name, "status": "ok", "message": ""} for name in sorted(doctor.CORE_CHECKS)
    ] + [{"name": "Groq", "status": "fail", "message": "Auth failed (HTTP 401)"}]
    buf = io.StringIO()
    doctor._render_results(results, Console(file=buf, width=120, color_system=None))
    banner = buf.getvalue().strip().splitlines()[0]
    assert "FAIL" not in banner
    assert "NOTE" in banner


def test_banner_says_fail_when_a_core_check_fails():
    results = [{"name": "Core Dependencies", "status": "fail", "message": "missing: rich"}]
    buf = io.StringIO()
    doctor._render_results(results, Console(file=buf, width=120, color_system=None))
    assert "FAIL" in buf.getvalue().strip().splitlines()[0]
