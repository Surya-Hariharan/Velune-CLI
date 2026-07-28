"""Tests for decorator/base-class/constructor-param metadata capture in
RepositorySnapshotParser, across both the tree-sitter and plain-ast.parse
fallback paths.

This is the structural evidence CodebaseAnalyzer's shape-based layer
fallback reads (see test_structural_shape_fingerprinting.py) — decorators
feed API/routing detection, base classes feed ORM/data-model detection,
and __init__ parameter types feed dependency-injection/services detection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.repository.parser import RepositorySnapshotParser

CODE = (
    "from fastapi import FastAPI\n\n"
    "app = FastAPI()\n\n\n"
    '@app.get("/items")\n'
    "def list_items():\n    return []\n\n\n"
    "class ItemService:\n"
    "    def __init__(self, repo: ItemRepository, client: ExternalApiClient):\n"
    "        self.repo = repo\n\n\n"
    "class Item(Base):\n"
    '    __tablename__ = "items"\n'
)


def _parse_with(force_ast_fallback: bool):
    parser = RepositorySnapshotParser()
    if force_ast_fallback:
        parser._ensure_loaded = lambda: False
    return parser.parse(Path("x.py"), CODE)


@pytest.mark.parametrize("force_ast_fallback", [False, True], ids=["tree-sitter", "ast-fallback"])
def test_decorator_capture(force_ast_fallback):
    symbols, _ = _parse_with(force_ast_fallback)
    list_items = next(s for s in symbols if s.name == "list_items")
    assert list_items.metadata.get("decorators") == ["app.get"]


@pytest.mark.parametrize("force_ast_fallback", [False, True], ids=["tree-sitter", "ast-fallback"])
def test_base_class_capture(force_ast_fallback):
    symbols, _ = _parse_with(force_ast_fallback)
    item = next(s for s in symbols if s.name == "Item")
    assert item.metadata.get("bases") == ["Base"]


@pytest.mark.parametrize("force_ast_fallback", [False, True], ids=["tree-sitter", "ast-fallback"])
def test_constructor_param_type_capture(force_ast_fallback):
    symbols, _ = _parse_with(force_ast_fallback)
    init_methods = [s for s in symbols if s.name == "__init__"]
    assert len(init_methods) == 1
    param_types = init_methods[0].metadata.get("param_types")
    assert param_types == ["ItemRepository", "ExternalApiClient"]


@pytest.mark.parametrize("force_ast_fallback", [False, True], ids=["tree-sitter", "ast-fallback"])
def test_plain_function_has_no_structural_metadata(force_ast_fallback):
    symbols, _ = _parse_with(force_ast_fallback)
    plain = next(s for s in symbols if s.name == "ItemService")
    assert plain.metadata == {}


def test_decorator_with_multiple_stacked_decorators():
    code = "@app.get(\"/x\")\n@cache(ttl=60)\ndef f():\n    return 1\n"
    parser = RepositorySnapshotParser()
    symbols, _ = parser.parse(Path("x.py"), code)
    f = next(s for s in symbols if s.name == "f")
    assert f.metadata.get("decorators") == ["app.get", "cache"]


def test_bare_name_decorator():
    code = "@staticmethod\ndef f():\n    return 1\n"
    parser = RepositorySnapshotParser()
    symbols, _ = parser.parse(Path("x.py"), code)
    f = next(s for s in symbols if s.name == "f")
    assert f.metadata.get("decorators") == ["staticmethod"]
