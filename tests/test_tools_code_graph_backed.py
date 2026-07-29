"""Coverage for the knowledge-graph-backed code-navigation tools.

Prior to this, ``go_to_definition``/``symbol_search`` re-parsed files from
scratch on every call (unable to resolve a cross-file definition) and
``find_references`` hardcoded ``file_pattern="*.py"`` (silently empty on any
non-Python repo). These tests lock in the graph-first behavior and the
fallback-when-unavailable safety net.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.kernel.registry import get_container
from velune.knowledge.schemas import KnowledgeNode, NodeType
from velune.tools.code.navigate import FindReferences, GoToDefinition
from velune.tools.code.search import SymbolSearch


class _FakeKnowledgeQuery:
    """Minimal stand-in exposing only the surface these tools call."""

    def __init__(self, nodes: list[KnowledgeNode]) -> None:
        self._nodes = nodes

    async def find_by_label(self, label: str) -> list[KnowledgeNode]:
        needle = label.lower()
        return [n for n in self._nodes if n.label.lower().startswith(needle)]


@pytest.fixture(autouse=True)
def _clean_container():
    """Isolate the global service container across tests in this module."""
    container = get_container()
    yield container
    container._singletons.pop("runtime.knowledge_query", None)  # noqa: SLF001


def test_go_to_definition_resolves_cross_file_via_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symbol defined in a different file than the reference resolves via the graph.

    A single-file re-parse structurally cannot do this — the definition
    doesn't live in the file being read.
    """
    monkeypatch.chdir(tmp_path)
    ref_file = tmp_path / "caller.py"
    ref_file.write_text("from helpers import build\nbuild()\n", encoding="utf-8")
    (tmp_path / "helpers.py").write_text("def build():\n    pass\n", encoding="utf-8")

    node = KnowledgeNode(
        id="function:helpers.py:build",
        node_type=NodeType.FUNCTION,
        label="build",
        file_path="helpers.py",
        line_start=1,
    )
    get_container().register_instance("runtime.knowledge_query", _FakeKnowledgeQuery([node]))

    tool = GoToDefinition(workspace=tmp_path)
    import asyncio

    result = asyncio.run(tool.execute(symbol_name="build", file_path="caller.py", line=2))

    assert result is not None
    assert result["file"] == "helpers.py"
    assert result["line"] == 1
    assert result["kind"] == "function"


def test_go_to_definition_falls_back_without_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No knowledge_query registered -> falls back to the original single-file parse."""
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "mod.py"
    target.write_text("def local_symbol():\n    pass\n", encoding="utf-8")

    tool = GoToDefinition(workspace=tmp_path)
    import asyncio

    result = asyncio.run(tool.execute(symbol_name="local_symbol", file_path="mod.py", line=1))

    assert result is not None
    assert result["kind"] == "function"


def test_symbol_search_prefers_graph_when_available(tmp_path: Path) -> None:
    node = KnowledgeNode(
        id="class:widgets.py:Widget",
        node_type=NodeType.CLASS,
        label="Widget",
        file_path="widgets.py",
        line_start=5,
    )
    get_container().register_instance("runtime.knowledge_query", _FakeKnowledgeQuery([node]))

    tool = SymbolSearch(workspace=tmp_path)
    import asyncio

    results = asyncio.run(tool.execute(symbol_name="Widget"))

    assert results == [{"name": "Widget", "kind": "class", "file": "widgets.py", "line": 5}]


def test_symbol_search_falls_back_to_scan_without_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.py").write_text("class Thing:\n    pass\n", encoding="utf-8")

    tool = SymbolSearch(workspace=tmp_path)
    import asyncio

    results = asyncio.run(tool.execute(symbol_name="Thing"))

    assert len(results) == 1
    assert results[0]["name"] == "Thing"


def test_find_references_no_longer_hardcoded_to_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A .ts file reference must be found — the old hardcoded '*.py' pattern missed it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.ts").write_text("function useToken() { return TOKEN; }\n", encoding="utf-8")

    tool = FindReferences(workspace=tmp_path)
    import asyncio

    results = asyncio.run(tool.execute(symbol_name="TOKEN"))

    assert any("app.ts" in (r.get("file") or r.get("path") or "") for r in results)
