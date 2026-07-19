"""Tests for velune.cli.rendering.diff_fragments and the diff_stats helper."""

from __future__ import annotations

from pathlib import Path

from velune.cli.rendering.diff_fragments import render_diff_fragments
from velune.execution.diff_preview import FileDiff, compute_file_diff, diff_stats


def _diff(original: str, proposed: str, *, new: bool = False) -> FileDiff:
    return FileDiff(
        path=Path("x.py"),
        original=original,
        proposed=proposed,
        is_new_file=new,
        is_deletion=(proposed == ""),
    )


def _texts(lines: list[list[tuple[str, str]]]) -> list[str]:
    return ["".join(t for _s, t in frags) for frags in lines]


def _styles(lines: list[list[tuple[str, str]]]) -> set[str]:
    return {s for frags in lines for s, _t in frags}


# ── diff_stats ───────────────────────────────────────────────────────────────


def test_diff_stats_counts_additions_and_removals():
    diff = _diff("a\nb\nc\n", "a\nX\nc\nd\n")
    assert diff_stats(diff) == (2, 1)  # b→X is one add + one del, plus d


def test_diff_stats_new_file_counts_all_lines_as_added():
    diff = _diff("", "one\ntwo\nthree\n", new=True)
    assert diff_stats(diff) == (3, 0)


def test_diff_stats_deletion_counts_all_lines_as_removed():
    diff = _diff("one\ntwo\n", "")
    assert diff_stats(diff) == (0, 2)


def test_compute_file_diff_flags_new_and_existing(tmp_path):
    fresh = compute_file_diff(tmp_path / "new.py", "x = 1\n")
    assert fresh.is_new_file and not fresh.is_deletion

    existing = tmp_path / "old.py"
    existing.write_text("x = 1\n", encoding="utf-8")
    mod = compute_file_diff(existing, "x = 2\n")
    assert not mod.is_new_file
    assert mod.original == "x = 1\n"


# ── render_diff_fragments ────────────────────────────────────────────────────


def test_modification_renders_styled_add_del_and_hunk_lines():
    lines = render_diff_fragments(_diff("a\nb\nc\n", "a\nX\nc\n"), width=100)
    texts = _texts(lines)
    styles = _styles(lines)
    assert any(t.lstrip().startswith("@@") for t in texts)
    assert any(t.lstrip().startswith("-b") for t in texts)
    assert any(t.lstrip().startswith("+X") for t in texts)
    assert "class:diff.add" in styles
    assert "class:diff.del" in styles
    assert "class:diff.hunk" in styles
    # ---/+++ file headers are skipped; the tool card already names the file.
    assert not any(t.lstrip().startswith(("---", "+++")) for t in texts)


def test_block_truncation_appends_more_lines_tail():
    original = "\n".join(f"line{i}" for i in range(120)) + "\n"
    proposed = "\n".join(f"line{i}x" for i in range(120)) + "\n"
    lines = render_diff_fragments(_diff(original, proposed), width=200, max_lines=10)
    texts = _texts(lines)
    assert len(lines) == 11  # 10 body lines + truncation tail
    assert "more lines" in texts[-1]
    assert "class:diff.meta" in _styles([lines[-1]])


def test_new_file_renders_plus_prefixed_content():
    lines = render_diff_fragments(_diff("", "a = 1\nb = 2\n", new=True), width=100)
    texts = _texts(lines)
    assert len(lines) == 2
    assert all(t.lstrip().startswith("+ ") for t in texts)
    assert _styles(lines) == {"class:diff.add"}


def test_new_file_truncates_to_max_lines():
    content = "\n".join(f"l{i}" for i in range(50)) + "\n"
    lines = render_diff_fragments(_diff("", content, new=True), width=100, max_lines=5)
    assert len(lines) == 6
    assert "+45 more lines" in _texts(lines)[-1]


def test_deletion_renders_single_summary_line():
    lines = render_diff_fragments(_diff("a\nb\nc\n", ""), width=100)
    assert len(lines) == 1
    text = _texts(lines)[0]
    assert "deleted" in text and "3 lines" in text
    assert _styles(lines) == {"class:diff.del"}


def test_long_lines_are_clamped_to_width():
    proposed = "x" * 500 + "\n"
    lines = render_diff_fragments(_diff("", proposed, new=True), width=60)
    for text in _texts(lines):
        assert len(text) <= 60
        assert text.endswith("…")


def test_identical_content_reports_no_changes():
    lines = render_diff_fragments(_diff("same\n", "same\n"), width=100)
    assert "(no changes)" in _texts(lines)[0]
