"""Tests for structural-shape fingerprinting in CodebaseAnalyzer.

``docs/repository-intelligence-baseline.md`` §6.6/§8: a fixed folder-name
vocabulary (``_GENERIC_LAYERS``) produces zero features — and an "Unknown"
architecture pattern — for single-letter, abbreviated, or non-English
directory names. The ``poorly_named`` benchmark repo saw all 4 files fall
into the "other" bucket purely because the heuristics never got past the
path string.

``CodebaseAnalyzer._reclassify_by_structural_shape`` rescues "other"-bucketed
files using content shape instead: decorator-based routing (``@router.get``),
ORM-style base classes (``class Foo(Base)``), and dependency-injection
constructors (``def __init__(self, x: FooService)``) — read from
``RepositorySymbol.metadata``, populated by the parser (parser.py's
``_dotted_names``/``_leading_decorator_texts``/``__init__`` param-type
capture).
"""

from __future__ import annotations

from pathlib import Path

from velune.repository.analyzer import CodebaseAnalyzer
from velune.repository.parser import RepositorySnapshotParser


def _symbols_for(root: Path, rel_path: str, code: str):
    """Parse *code* as if it were workspace file *rel_path* under *root*,
    returning symbols with file_path set the way the real pipeline does
    (absolute path, since that's what parser.parse() is actually called
    with — see indexer.py)."""
    abs_path = root / rel_path
    parser = RepositorySnapshotParser()
    symbols, _ = parser.parse(abs_path, code)
    return symbols


def test_decorator_routing_rescues_a_terse_directory_name(tmp_path):
    code = (
        "from fastapi import APIRouter\n\n"
        "router = APIRouter()\n\n\n"
        '@router.get("/things")\n'
        "def y():\n    return []\n"
    )
    symbols = _symbols_for(tmp_path, "a/x.py", code)

    analyzer = CodebaseAnalyzer(tmp_path)
    layers = analyzer.classify_architecture_layers(["a/x.py"], None, symbols)

    assert "a/x.py" in layers["api"]
    assert "a/x.py" not in layers["other"]


def test_orm_base_class_rescues_a_terse_directory_name(tmp_path):
    code = (
        "from sqlalchemy.orm import declarative_base\n\n"
        "Base = declarative_base()\n\n\n"
        'class Thing(Base):\n    __tablename__ = "things"\n'
    )
    symbols = _symbols_for(tmp_path, "b/z.py", code)

    analyzer = CodebaseAnalyzer(tmp_path)
    layers = analyzer.classify_architecture_layers(["b/z.py"], None, symbols)

    assert "b/z.py" in layers["data"]
    assert "b/z.py" not in layers["other"]


def test_di_constructor_rescues_a_terse_directory_name(tmp_path):
    code = (
        "class ThingService:\n"
        "    def __init__(self, repo: ThingRepository):\n"
        "        self.repo = repo\n"
    )
    symbols = _symbols_for(tmp_path, "c/w.py", code)

    analyzer = CodebaseAnalyzer(tmp_path)
    layers = analyzer.classify_architecture_layers(["c/w.py"], None, symbols)

    assert "c/w.py" in layers["services"]
    assert "c/w.py" not in layers["other"]


def test_no_structural_match_stays_in_other(tmp_path):
    code = "def plain_function():\n    return 1\n"
    symbols = _symbols_for(tmp_path, "d/q.py", code)

    analyzer = CodebaseAnalyzer(tmp_path)
    layers = analyzer.classify_architecture_layers(["d/q.py"], None, symbols)

    assert "d/q.py" in layers["other"]


def test_folder_name_match_is_not_overridden_by_structural_rescue(tmp_path):
    """A file already classified by folder name (e.g. under /models/) must
    not be re-checked or moved by the structural pass — it's only a
    fallback for files folder-matching couldn't place."""
    code = "def plain_function():\n    return 1\n"
    symbols = _symbols_for(tmp_path, "src/models/q.py", code)

    analyzer = CodebaseAnalyzer(tmp_path)
    layers = analyzer.classify_architecture_layers(["src/models/q.py"], None, symbols)

    assert "src/models/q.py" in layers["data"]  # matched by folder name, not shape
    assert "src/models/q.py" not in layers["other"]


def test_velune_layer_set_is_not_affected_by_structural_rescue(tmp_path):
    """The Velune-specific layer set doesn't have api/data/services buckets
    — the structural pass must be a no-op there, not raise."""
    code = (
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n\n\n"
        '@router.get("/x")\ndef y():\n    return []\n'
    )
    symbols = _symbols_for(tmp_path, "velune/kernel/__init__.py", code)

    analyzer = CodebaseAnalyzer(tmp_path)
    layers = analyzer.classify_architecture_layers(
        ["velune/kernel/__init__.py"], None, symbols
    )
    assert "api" not in layers  # Velune layer set, no generic buckets
