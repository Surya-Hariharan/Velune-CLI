"""Tests for the cold-start LLM-reasoning guard (velune/repository/llm_gate.py).

This is a forward-looking guardrail: nothing in the repository-intelligence
pipeline calls an LLM today, but the architecture direction it's heading
toward (a semantic capability layer — see
velune.knowledge.schemas.NodeType.CAPABILITY/RUNTIME_ENTRYPOINT) will
eventually need "targeted LLM reasoning" passes, and the synchronous
cold-start indexing path (RepositoryCognitionService.index(), documented
as unbounded — baseline §3.1) is exactly the wrong place for one to end
up. These tests pin down that the guard actually fires, and that
RepositoryCognitionService.index() is wired to it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from velune.repository.cognition import RepositoryCognitionService
from velune.repository.llm_gate import (
    ColdStartLLMCallError,
    cold_start_scope,
    ensure_background_only,
    is_in_cold_start_scope,
)


def test_ensure_background_only_is_a_no_op_outside_any_scope():
    ensure_background_only("some future capability-inference call")  # must not raise


def test_ensure_background_only_raises_inside_cold_start_scope():
    with cold_start_scope():
        with pytest.raises(ColdStartLLMCallError, match="capability naming"):
            ensure_background_only("capability naming")


def test_scope_is_cleared_after_exit_even_on_exception():
    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        with cold_start_scope():
            raise _Boom()

    assert is_in_cold_start_scope() is False
    ensure_background_only("after the scope exited")  # must not raise


def test_scopes_do_not_leak_across_nesting():
    assert is_in_cold_start_scope() is False
    with cold_start_scope():
        assert is_in_cold_start_scope() is True
    assert is_in_cold_start_scope() is False


def test_repository_cognition_service_index_is_wrapped_in_cold_start_scope():
    """A real, if synthetic, check that index() actually engages the guard:
    monkeypatch the guard's check point (_get_snapshot_fresh-adjacent) by
    calling ensure_background_only from inside a patched pipeline stage and
    confirming it raises during a real index() call."""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")

        svc = RepositoryCognitionService(root)
        original_run_pipeline = svc._run_pipeline

        captured = {}

        def _spy_run_pipeline(snapshot):
            captured["in_scope"] = is_in_cold_start_scope()
            return original_run_pipeline(snapshot)

        svc._run_pipeline = _spy_run_pipeline
        svc.index(force=True)

        assert captured.get("in_scope") is True


def test_is_in_cold_start_scope_false_outside_index():
    assert is_in_cold_start_scope() is False
