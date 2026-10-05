"""A broken [parsing] install must be visible, and parsing must still work.

Regression: with tree-sitter 0.21/0.22 (allowed by the old floors) every
`Language(grammar.language())` call raised, the errors were swallowed by a
bare `except: pass`, and HAS_TREE_SITTER stayed True with zero grammars —
repository parsing silently degraded and `velune doctor` either said "ok" for
a partial failure or gave no fix. Now each failure is logged, availability
means "at least one grammar loaded", and doctor names the broken grammars.
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path

import pytest

from velune.cli.commands.doctor import _check_treesitter
from velune.repository import parser as ts


@pytest.fixture
def fresh_loader(monkeypatch):
    """Reset the lazy loader's module-level cache for one test."""
    monkeypatch.setattr(ts, "HAS_TREE_SITTER", None)
    monkeypatch.setattr(ts, "_TS_LANGUAGES", {})
    monkeypatch.setattr(ts, "_TS_IMPORT_ERROR", None)
    monkeypatch.setattr(ts, "_TS_LOAD_ERRORS", [])
    monkeypatch.setattr(ts, "_TS_PARSER_CLS", None)


def _install_fake_tree_sitter(monkeypatch, *, broken: set[str]):
    """Fake tree_sitter + grammar modules; grammars in *broken* fail to load."""

    class Language:
        def __init__(self, handle):
            if handle in broken:
                raise TypeError("an integer is required")
            self.name = handle

    core = types.ModuleType("tree_sitter")
    core.Language = Language
    core.Parser = object
    monkeypatch.setitem(sys.modules, "tree_sitter", core)
    for mod_name, attrs in {
        "tree_sitter_python": {"language": "python"},
        "tree_sitter_typescript": {"language_typescript": "typescript"},
        "tree_sitter_go": {"language": "go"},
        "tree_sitter_rust": {"language": "rust"},
    }.items():
        mod = types.ModuleType(mod_name)
        for attr, handle in attrs.items():
            setattr(mod, attr, lambda h=handle: h)
        monkeypatch.setitem(sys.modules, mod_name, mod)


def test_all_grammars_broken_is_unavailable_and_logged(fresh_loader, monkeypatch, caplog):
    _install_fake_tree_sitter(monkeypatch, broken={"python", "typescript", "go", "rust"})
    with caplog.at_level(logging.WARNING, logger="velune.repository.parser"):
        assert ts._ensure_tree_sitter() is False
    assert "tree-sitter grammars failed to load" in caplog.text
    assert "an integer is required" in caplog.text

    # Parsing still works through the ast fallback.
    symbols, _ = ts.RepositorySnapshotParser().parse(Path("a.py"), "def f():\n    return 1\n")
    assert any(s.name == "f" for s in symbols)

    result = _check_treesitter()
    assert result["status"] == "warn"
    assert "Grammars failed to load" in result["message"]
    assert "pip install --upgrade" in result["message"]


def test_partial_failure_is_reported_not_ok(fresh_loader, monkeypatch):
    _install_fake_tree_sitter(monkeypatch, broken={"go"})
    assert ts._ensure_tree_sitter() is True
    result = _check_treesitter()
    assert result["status"] == "warn"
    assert "go (TypeError" in result["message"]
    assert "python" in result["message"]  # listed as loaded


def test_missing_extra_is_an_optional_warning_with_install_hint(fresh_loader, monkeypatch):
    monkeypatch.setitem(sys.modules, "tree_sitter_go", None)  # import → ImportError
    assert ts._ensure_tree_sitter() is False
    result = _check_treesitter()
    assert result["status"] == "warn"
    assert "parsing] extra not installed" in result["message"]


def test_all_grammars_loaded_is_ok(fresh_loader, monkeypatch):
    _install_fake_tree_sitter(monkeypatch, broken=set())
    assert ts._ensure_tree_sitter() is True
    assert _check_treesitter()["status"] == "ok"
