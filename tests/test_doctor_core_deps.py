"""`velune doctor` core-dependency check reads the real declared requirements.

Regression: the check used a hand-kept list that included qdrant_client —
part of the optional [rag] extra — so every lean `pip install velune-cli`
reported "fail: Missing core dependencies". It now reads the installed
package metadata, ignores extras / other-Python marker lines, and flags
versions below the floor (a stale copy left by another tool).
"""

from __future__ import annotations

import importlib.metadata as md

import pytest

from velune.cli.commands.doctor import _check_core_dependencies

pytest.importorskip("packaging")

_REQS = [
    "typer>=0.16.0",
    "rich>=13.9.0",
    "numpy<3,>=1.26.0; python_version < '3.0'",  # never applies → ignored
    "numpy<3,>=1.26.0; python_version >= '3.0'",
    "qdrant-client>=1.7.0; extra == 'rag'",  # optional extra → ignored
]


def _fake_metadata(monkeypatch, installed: dict[str, str]):
    monkeypatch.setattr(md, "requires", lambda dist: list(_REQS))

    def _version(name: str) -> str:
        if name not in installed:
            raise md.PackageNotFoundError(name)
        return installed[name]

    monkeypatch.setattr(md, "version", _version)


def test_lean_install_without_rag_extra_is_ok(monkeypatch):
    _fake_metadata(monkeypatch, {"typer": "0.27.0", "rich": "15.0.0", "numpy": "2.3.0"})
    result = _check_core_dependencies()
    assert result["status"] == "ok", result["message"]


def test_missing_core_dependency_fails_with_fix(monkeypatch):
    _fake_metadata(monkeypatch, {"typer": "0.27.0", "numpy": "2.3.0"})
    result = _check_core_dependencies()
    assert result["status"] == "fail"
    assert "missing: rich" in result["message"]
    assert "pip install --upgrade velune-cli" in result["message"]


def test_stale_version_below_floor_is_reported(monkeypatch):
    _fake_metadata(monkeypatch, {"typer": "0.9.0", "rich": "15.0.0", "numpy": "2.3.0"})
    result = _check_core_dependencies()
    assert result["status"] == "fail"
    assert "typer 0.9.0" in result["message"]
    assert "qdrant" not in result["message"]


def test_real_installed_metadata_passes_in_dev_env():
    # The dev/CI environment installs velune-cli, so its own check must pass.
    try:
        md.requires("velune-cli")
    except md.PackageNotFoundError:
        pytest.skip("velune-cli not installed in this environment")
    assert _check_core_dependencies()["status"] == "ok"


def test_without_packaging_marker_lines_are_skipped_not_reported_missing(monkeypatch):
    """Regression: a fresh pip install has no `packaging` (until it became a
    core dependency); the presence-only fallback then ignored markers and
    reported `tomli; python_version < '3.11'` as missing on Python 3.14."""
    import sys

    monkeypatch.setitem(sys.modules, "packaging.requirements", None)  # → ImportError
    monkeypatch.setattr(
        md, "requires", lambda dist: ["typer>=0.16.0", "tomli>=2.0.1; python_version < '3.0'"]
    )
    monkeypatch.setattr(md, "version", lambda name: {"typer": "0.27.0"}[name])
    result = _check_core_dependencies()
    assert result["status"] == "ok", result["message"]


def test_packaging_is_a_declared_core_dependency():
    """doctor's version/marker checks depend on it, so it must ship in the lean install."""
    try:
        reqs = md.requires("velune-cli") or []
    except md.PackageNotFoundError:
        pytest.skip("velune-cli not installed in this environment")
    assert any(r.split(";")[0].strip().startswith("packaging") and "extra" not in r for r in reqs)
