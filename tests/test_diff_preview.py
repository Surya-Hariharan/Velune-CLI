"""Tests for the diff preview system."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from velune.execution.diff_preview import DiffDecision, DiffPreview, FileDiff


@pytest.fixture
def preview(tmp_path):
    console = MagicMock()
    return DiffPreview(console), tmp_path


def test_compute_diff_new_file(preview):
    p, tmp = preview
    new_file = tmp / "new.py"
    diff = p.compute_diff(new_file, "print('hello')")
    assert diff.is_new_file is True
    assert diff.original == ""
    assert diff.proposed == "print('hello')"


def test_compute_diff_existing_file(preview, tmp_path):
    p, _ = preview
    f = tmp_path / "existing.py"
    f.write_text("x = 1")
    diff = p.compute_diff(f, "x = 2")
    assert diff.is_new_file is False
    assert diff.original == "x = 1"
    assert diff.proposed == "x = 2"


def test_compute_diff_deletion(preview, tmp_path):
    p, _ = preview
    f = tmp_path / "gone.py"
    f.write_text("old content")
    diff = p.compute_diff(f, "")
    assert diff.is_deletion is True
    assert diff.is_new_file is False


def test_detect_language():
    console = MagicMock()
    p = DiffPreview(console)
    assert p._detect_language(Path("foo.py")) == "python"
    assert p._detect_language(Path("bar.ts")) == "typescript"
    assert p._detect_language(Path("baz.unknown")) == "text"


@pytest.mark.asyncio
async def test_auto_accept_skips_prompt(preview):
    p, tmp = preview
    f = tmp / "auto.py"
    decision = await p.preview_and_confirm(f, "content", auto_accept=True)
    assert decision == DiffDecision.ACCEPT


@pytest.mark.asyncio
async def test_module_level_auto_accept(preview, tmp_path):
    import velune.execution.diff_preview as dp

    dp.configure(auto_accept=True)
    try:
        p, _ = preview
        f = tmp_path / "mod_auto.py"
        decision = await p.preview_and_confirm(f, "content", auto_accept=False)
        assert decision == DiffDecision.ACCEPT
    finally:
        dp.configure(auto_accept=False)  # reset so other tests are unaffected


@pytest.mark.asyncio
async def test_reject_skips_write(preview, tmp_path):
    p, _ = preview
    f = tmp_path / "reject.py"
    with patch("rich.prompt.Prompt.ask", return_value="r"):
        decision = await p.preview_and_confirm(f, "content", auto_accept=False)
    assert decision == DiffDecision.REJECT
    assert not f.exists()


@pytest.mark.asyncio
async def test_accept_response(preview, tmp_path):
    p, _ = preview
    f = tmp_path / "accept.py"
    with patch("rich.prompt.Prompt.ask", return_value="a"):
        decision = await p.preview_and_confirm(f, "content", auto_accept=False)
    assert decision == DiffDecision.ACCEPT


def test_render_diff_new_file_calls_console(preview, tmp_path):
    p, _ = preview
    f = tmp_path / "render_new.py"
    diff = FileDiff(path=f, original="", proposed="x = 1", is_new_file=True, is_deletion=False)
    p.render_diff(diff)
    p.console.print.assert_called()


def test_render_diff_deletion_calls_console(preview, tmp_path):
    p, _ = preview
    f = tmp_path / "render_del.py"
    f.write_text("old")
    diff = FileDiff(path=f, original="old", proposed="", is_new_file=False, is_deletion=True)
    p.render_diff(diff)
    p.console.print.assert_called()
