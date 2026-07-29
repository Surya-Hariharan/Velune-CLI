"""Unit tests for Jupyter notebook parsing (RepositorySnapshotParser).

``docs/repository-intelligence-baseline.md`` §6.3: ``.ipynb`` was absent from
the discovery allowlist entirely, so every downstream feature (symbols,
import graph, blast radius) operated as if notebooks didn't exist — the
sharpest single finding for ML-workflow repos. Fixed by adding ``.ipynb`` to
``schemas.EXTENSION_LANGUAGE_MAP`` and reconstructing synthetic Python
source from code cells in ``RepositorySnapshotParser._extract_notebook_source``.
"""

from __future__ import annotations

import json
from pathlib import Path

from velune.repository.parser import RepositorySnapshotParser
from velune.repository.schemas import RepositorySymbolKind


def _notebook(cells: list[dict], kernel_language: str = "python") -> str:
    return json.dumps(
        {
            "cells": cells,
            "metadata": {"kernelspec": {"language": kernel_language}},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


def test_cells_reordered_by_execution_count_not_position():
    """A cell defined later in the file but run first must come first in
    the reconstructed source — logical (run) order, not document order."""
    raw = _notebook(
        [
            {"cell_type": "code", "execution_count": 2, "source": ["def train():\n    return helper()\n"]},
            {"cell_type": "code", "execution_count": 1, "source": ["def helper():\n    return 42\n"]},
        ]
    )
    parser = RepositorySnapshotParser()
    symbols, _ = parser.parse(Path("nb.ipynb"), raw)
    names = [s.name for s in symbols if s.kind == RepositorySymbolKind.FUNCTION]
    assert names.index("helper") < names.index("train")


def test_unexecuted_cells_appended_after_executed_ones():
    """Cells with no execution_count (never run, or outputs cleared before
    commit) are still represented — just ordered last, since there's no run
    order signal for them."""
    raw = _notebook(
        [
            {"cell_type": "code", "execution_count": None, "source": ["def never_run():\n    pass\n"]},
            {"cell_type": "code", "execution_count": 1, "source": ["def ran():\n    pass\n"]},
        ]
    )
    parser = RepositorySnapshotParser()
    symbols, _ = parser.parse(Path("nb.ipynb"), raw)
    names = [s.name for s in symbols if s.kind == RepositorySymbolKind.FUNCTION]
    assert names == ["ran", "never_run"]


def test_markdown_cells_are_ignored():
    raw = _notebook(
        [
            {"cell_type": "markdown", "source": ["# Title\n", "some prose\n"]},
            {"cell_type": "code", "execution_count": 1, "source": ["def f():\n    pass\n"]},
        ]
    )
    parser = RepositorySnapshotParser()
    symbols, _ = parser.parse(Path("nb.ipynb"), raw)
    assert [s.name for s in symbols] == ["f"]


def test_non_python_kernel_degrades_to_zero_symbols_not_a_crash():
    raw = _notebook(
        [{"cell_type": "code", "execution_count": 1, "source": ["let x = 1;"]}],
        kernel_language="javascript",
    )
    parser = RepositorySnapshotParser()
    symbols, edges = parser.parse(Path("nb.ipynb"), raw)
    assert symbols == []
    assert edges == []


def test_malformed_json_degrades_to_zero_symbols_not_a_crash():
    parser = RepositorySnapshotParser()
    symbols, edges = parser.parse(Path("broken.ipynb"), "{not valid json")
    assert symbols == []
    assert edges == []


def test_source_as_single_string_not_list_is_handled():
    """nbformat allows a cell's ``source`` to be either a list of lines or a
    single string — both must work."""
    raw = _notebook([{"cell_type": "code", "execution_count": 1, "source": "def f():\n    pass\n"}])
    parser = RepositorySnapshotParser()
    symbols, _ = parser.parse(Path("nb.ipynb"), raw)
    assert [s.name for s in symbols] == ["f"]
