"""Issues #35 and #36: mixed line endings in edit_file, and +A/-R counts for -- and ++ content.

#35 ``plan_edit`` used only the first line-ending form of a multi-line ``old_string`` that matched
    anything, so in a file mixing LF, CRLF and CR the other occurrences were neither counted for
    the uniqueness check nor replaced by ``replace_all``.
#36 The +A/-R statistics skipped every line that starts with ``+++`` or ``---``, which also dropped
    real content lines such as a removed YAML ``---`` or an added ``++x``.
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from velune.execution.diff_preview import FileDiff, diff_stats
from velune.tools.filesystem import mutation as mut
from velune.tools.filesystem.edit import EditError, EditFile, plan_edit


def _file(text: str) -> mut.TextFile:
    style, newline = mut.newline_style(text)
    return mut.TextFile(Path("t.txt"), b"", text, "utf-8", b"", newline, style, "0")


MIXED = "a\nb\nsep\na\r\nb\r\n"
PURE_CR = "a\rb\rsep\ra\rb\r"


# ── #35 ────────────────────────────────────────────────────────────────────────────────────────


def test_replace_all_replaces_every_occurrence_in_either_ending():
    new_text, count = plan_edit(_file(MIXED), "a\nb", "X\nY", replace_all=True)
    assert count == 2
    # Each replacement keeps the endings of the text it replaced; nothing else changes.
    assert new_text == "X\nY\nsep\nX\r\nY\r\n"


def test_the_default_edit_is_rejected_as_ambiguous_when_the_forms_are_split():
    with pytest.raises(EditError) as raised:
        plan_edit(_file(MIXED), "a\nb", "X\nY")
    assert "matches 2 places (lines 1, 4)" in str(raised.value)


def test_a_unique_occurrence_in_the_minority_ending_is_found_and_replaced_alone():
    text = "keep\nkeep\nx\r\ny\r\nkeep\n"
    new_text, count = plan_edit(_file(text), "x\ny", "Z\nW")
    assert (new_text, count) == ("keep\nkeep\nZ\r\nW\r\nkeep\n", 1)


def test_the_count_is_exact_with_many_occurrences_of_both_forms():
    text = "p\nq\n" * 50 + "p\r\nq\r\n" * 30
    new_text, count = plan_edit(_file(text), "p\nq", "P\nQ", replace_all=True)
    assert count == 80
    assert new_text == "P\nQ\n" * 50 + "P\r\nQ\r\n" * 30


def test_lone_cr_line_endings_are_matched_and_kept():
    new_text, count = plan_edit(_file(PURE_CR), "a\nb", "X\nY", replace_all=True)
    assert (new_text, count) == ("X\rY\rsep\rX\rY\r", 2)


def test_all_three_endings_in_one_file():
    text = "a\nb\n-\na\r\nb\r\n-\na\rb\r"
    new_text, count = plan_edit(_file(text), "a\nb", "X\nY", replace_all=True)
    assert count == 3
    assert new_text == "X\nY\n-\nX\r\nY\r\n-\nX\rY\r"


def test_a_crlf_is_never_split_into_a_cr_and_an_lf():
    # "\n\n" (a blank line) must not match the CR and LF of a single CRLF.
    with pytest.raises(EditError, match="was not found"):
        plan_edit(_file("a\r\nb"), "a\n\nb", "x")
    new_text, count = plan_edit(_file("a\r\n\r\nb"), "a\n\nb", "x")
    assert (new_text, count) == ("x", 1)


def test_the_text_around_a_match_is_untouched_byte_for_byte():
    text = "h1\r\nh2\nh3\rtarget\nline\r\nt1\nt2\r\n"
    new_text, _ = plan_edit(_file(text), "target\nline", "T\nL")
    assert new_text == "h1\r\nh2\nh3\rT\nL\r\nt1\nt2\r\n"


def test_a_replacement_with_a_different_line_count_takes_the_first_ending():
    new_text, _ = plan_edit(_file("a\r\nb\r\nz\n"), "a\nb", "1\n2\n3")
    assert new_text == "1\r\n2\r\n3\r\nz\n"
    new_text, _ = plan_edit(_file("a\nb\r\nz"), "a\nb", "single")
    assert new_text == "single\r\nz"


def test_a_block_that_mixes_endings_keeps_each_ending_when_the_line_count_is_unchanged():
    new_text, _ = plan_edit(_file("a\r\nb\nc\rz"), "a\nb\nc", "A\nB\nC")
    assert new_text == "A\r\nB\nC\rz"


def test_pure_lf_and_pure_crlf_behave_as_before():
    assert plan_edit(_file("a\nb\nc\n"), "a\nb", "X\nY") == ("X\nY\nc\n", 1)
    assert plan_edit(_file("a\r\nb\r\nc\r\n"), "a\nb", "X\nY") == ("X\r\nY\r\nc\r\n", 1)
    # A needle written with CRLF itself finds the same text.
    assert plan_edit(_file("a\r\nb\r\nc\r\n"), "a\r\nb", "X\r\nY") == ("X\r\nY\r\nc\r\n", 1)


def test_single_line_needles_are_unchanged_by_mixed_endings():
    new_text, count = plan_edit(_file(MIXED), "sep", "S")
    assert (new_text, count) == ("a\nb\nS\na\r\nb\r\n", 1)
    new_text, count = plan_edit(_file(MIXED), "a", "A", replace_all=True)
    assert (new_text, count) == ("A\nb\nsep\nA\r\nb\r\n", 2)


def test_no_match_still_reports_not_found():
    with pytest.raises(EditError, match="was not found"):
        plan_edit(_file(MIXED), "a\nc", "x")
    with pytest.raises(EditError, match="was not found"):
        plan_edit(_file(MIXED), "zzz\nyyy", "x")


def test_a_needle_that_ends_or_starts_with_a_break_matches_whole_lines():
    new_text, count = plan_edit(_file("x\r\nkeep\nx\nkeep\r\n"), "x\n", "", replace_all=True)
    assert (new_text, count) == ("keep\nkeep\r\n", 2)
    new_text, count = plan_edit(_file("a\r\nb\n"), "\nb", "\nB")
    assert (new_text, count) == ("a\r\nB\n", 1)


def test_regex_metacharacters_in_the_needle_and_replacement_are_literal():
    text = "a.*b(c)\n[d]+\r\na.*b(c)\n[d]+\n"
    new_text, count = plan_edit(_file(text), "a.*b(c)\n[d]+", "ok\nok", replace_all=True)
    assert (new_text, count) == ("ok\nok\r\nok\nok\n", 2)
    new_text, _ = plan_edit(_file("a\nb"), "a\nb", "\\1\\g<0>\nz")
    assert new_text == "\\1\\g<0>\nz"


async def test_end_to_end_replace_all_on_a_mixed_file_preserves_bytes_and_counts(tmp_path):
    path = tmp_path / "m.txt"
    path.write_bytes(b"top\r\na\nb\nmid\r\na\r\nb\r\nlast\n")
    out = await EditFile(workspace=tmp_path).execute(
        file_path="m.txt", old_string="a\nb", new_string="X\nY", replace_all=True
    )
    assert path.read_bytes() == b"top\r\nX\nY\nmid\r\nX\r\nY\r\nlast\n"
    assert "2 replacements" in out
    assert "(+4 -4)" in out


async def test_end_to_end_default_edit_refuses_an_ambiguous_mixed_file(tmp_path):
    path = tmp_path / "m.txt"
    raw = b"a\nb\nsep\na\r\nb\r\n"
    path.write_bytes(raw)
    with pytest.raises(EditError, match="matches 2 places"):
        await EditFile(workspace=tmp_path).execute(
            file_path="m.txt", old_string="a\nb", new_string="X\nY"
        )
    assert path.read_bytes() == raw


async def test_encoding_and_bom_survive_a_mixed_ending_edit(tmp_path):
    path = tmp_path / "bom.txt"
    path.write_bytes(b"\xef\xbb\xbfcaf\xc3\xa9\nx\r\ny\nx\nz\r\n")
    await EditFile(workspace=tmp_path).execute(
        file_path="bom.txt", old_string="x\ny", new_string="X\nY"
    )
    assert path.read_bytes() == b"\xef\xbb\xbfcaf\xc3\xa9\nX\r\nY\nx\nz\r\n"
    utf16 = tmp_path / "u16.txt"
    utf16.write_bytes(b"\xff\xfe" + "a\nb\r\nc\na\r\nb\r\n".encode("utf-16-le"))
    await EditFile(workspace=tmp_path).execute(
        file_path="u16.txt", old_string="a\nb", new_string="A\nB", replace_all=True
    )
    assert utf16.read_bytes() == b"\xff\xfe" + "A\nB\r\nc\nA\r\nB\r\n".encode("utf-16-le")


def test_the_approval_preview_counts_every_form(tmp_path):
    (tmp_path / "m.txt").write_bytes(b"a\nb\nsep\na\r\nb\r\n")
    planned = EditFile(workspace=tmp_path).preview(
        {"file_path": "m.txt", "old_string": "a\nb", "new_string": "X\nY", "replace_all": True}
    )
    assert planned is not None
    assert planned[1] == "X\nY\nsep\nX\r\nY\r\n"


# ── #36 ────────────────────────────────────────────────────────────────────────────────────────


def _udiff(old: str, new: str) -> list[str]:
    return list(
        difflib.unified_diff(old.splitlines(), new.splitlines(), "a/f", "b/f", lineterm="", n=2)
    )


def _stats(old: str, new: str) -> tuple[int, int]:
    return diff_stats(FileDiff(Path("f"), old, new, False, False))


CASES = [
    pytest.param("a\n---\nb\n", "a\nb\n", (0, 1), id="removed-yaml-separator"),
    pytest.param("a\n-- note\nb\n", "a\nb\n", (0, 1), id="removed-sql-comment"),
    pytest.param("a\nb\n", "a\n++x\nb\n", (1, 0), id="added-plus-plus"),
    pytest.param("a\nb\n", "a\n+++x\nb\n", (1, 0), id="added-plus-plus-plus"),
    pytest.param("a\nb\n", "a\n---\nb\n", (1, 0), id="added-yaml-separator"),
    pytest.param("a\n--x\nb\n", "a\n++y\nb\n", (1, 1), id="replaced-dashes-by-pluses"),
    pytest.param("a\nb\nc\n", "a\nB\nc\n", (1, 1), id="ordinary-replace"),
    pytest.param("a\nb\n", "a\nb\nc\nd\n", (2, 0), id="ordinary-additions"),
    pytest.param("a\nb\nc\n", "a\n", (0, 2), id="ordinary-deletions"),
    pytest.param("a\nb\n", "a\nb\n", (0, 0), id="identical"),
    pytest.param("-\n+\n", "-\n+\nx\n", (1, 0), id="context-lines-that-look-like-changes"),
]


@pytest.mark.parametrize(("old", "new", "expected"), CASES)
def test_unified_diff_counts(old, new, expected):
    _, added, removed = mut.unified_diff(old, new, "f")
    assert (added, removed) == expected


@pytest.mark.parametrize(("old", "new", "expected"), CASES)
def test_diff_stats_counts(old, new, expected):
    assert _stats(old, new) == expected


@pytest.mark.parametrize(("old", "new", "expected"), CASES)
def test_unified_diff_and_diff_stats_agree(old, new, expected):
    _, added, removed = mut.unified_diff(old, new, "f")
    assert _stats(old, new) == (added, removed)


def test_a_diff_with_no_changes_has_no_header_to_skip():
    assert _udiff("a\nb\n", "a\nb\n") == []
    assert mut.count_changed_lines([]) == (0, 0)
    assert mut.unified_diff("", "", "f")[1:] == (0, 0)


def test_multiple_hunks_with_look_alike_header_content():
    lines = [f"line {n}" for n in range(40)]
    lines[2] = "--- removed-look-alike-header"
    lines[30] = "+++ added-look-alike-header"
    old = "\n".join(lines) + "\n"
    new_lines = list(lines)
    del new_lines[2]
    new_lines[29] = "++ changed"
    new = "\n".join(new_lines) + "\n"
    assert sum(1 for ln in _udiff(old, new) if ln.startswith("@@")) == 2
    _, added, removed = mut.unified_diff(old, new, "f")
    assert (added, removed) == (1, 2)
    assert _stats(old, new) == (1, 2)


def test_the_two_header_lines_are_never_counted():
    udiff = _udiff("a\n", "b\n")
    assert udiff[0].startswith("--- ") and udiff[1].startswith("+++ ")
    assert mut.count_changed_lines(udiff) == (1, 1)


def test_new_file_and_deletion_counts_are_unchanged():
    assert diff_stats(FileDiff(Path("f"), "", "--a\n++b\n", True, False)) == (2, 0)
    assert diff_stats(FileDiff(Path("f"), "--a\n++b\n", "", False, True)) == (0, 2)


async def test_edit_file_reports_the_removed_yaml_separator(tmp_path):
    (tmp_path / "c.yml").write_text("a: 1\n---\nb: 2\n", encoding="utf-8", newline="")
    out = await EditFile(workspace=tmp_path).execute(
        file_path="c.yml", old_string="---\n", new_string=""
    )
    assert out.splitlines()[0].startswith("edit_file OK c.yml (+0 -1) verified")
