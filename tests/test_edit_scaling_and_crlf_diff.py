"""edit_file: linear match counting (CodeRabbit #9) and an accurate CRLF approval diff (#11).

#9  ``_match_lines`` re-counted newlines from the start of the file for every match, so a file with
    many occurrences cost O(matches x size) on the event loop, even for ``replace_all`` which
    never uses line numbers. The checks below count *work* (characters examined) instead of
    measuring time, so they are deterministic under CI load.
#11 The approval diff read the original with universal newlines (LF) but compared it with a
    proposal that kept the file's CRLFs, so one edited line showed as a rewrite of every line.
"""

from __future__ import annotations

import tracemalloc
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from velune.execution.diff_preview import DiffPreview, compute_file_diff, diff_stats
from velune.execution.hunk_review import HunkReviewer
from velune.tools.filesystem import mutation as mut
from velune.tools.filesystem.edit import EditError, EditFile, plan_edit


def _file(text: str, newline: str = "\n", style: str = "lf") -> mut.TextFile:
    return mut.TextFile(Path("t.txt"), b"", text, "utf-8", b"", newline, style, "0")


class _Probe(str):
    """A str that records how much text ``count`` and ``find`` are asked to examine."""

    scanned = 0
    calls = 0

    def count(self, sub, start=0, end=None):  # type: ignore[override]
        end = len(self) if end is None else min(end, len(self))
        type(self).scanned += max(0, end - start)
        type(self).calls += 1
        return super().count(sub, start, end)

    def find(self, sub, start=0, end=None):  # type: ignore[override]
        end = len(self) if end is None else min(end, len(self))
        found = super().find(sub, start, end)
        # ``find`` only examines up to the match (or to the end when there is none).
        type(self).scanned += max(0, (found + len(sub) if found != -1 else end) - start)
        type(self).calls += 1
        return found


def _probe(text: str) -> _Probe:
    _Probe.scanned = 0
    _Probe.calls = 0
    return _Probe(text)


# ── #9 ─────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("occurrences", [5_000, 40_000])
def test_an_ambiguous_edit_is_rejected_with_the_true_count_and_the_first_lines(occurrences):
    text = "x\n" * occurrences
    with pytest.raises(EditError) as raised:
        plan_edit(_file(text), "x", "y")
    message = str(raised.value)
    assert f"matches {occurrences} places" in message
    assert "lines 1, 2, 3, 4, 5, 6, 7, 8, 9, 10…" in message


@pytest.mark.parametrize("occurrences", [5_000, 40_000])
def test_the_work_done_to_reject_an_ambiguous_edit_is_linear(occurrences):
    """Old behavior re-scanned from the start for each match: ~occurrences x size."""
    probe = _probe("x\n" * occurrences)
    with pytest.raises(EditError):
        plan_edit(_file(probe), "x", "y")
    assert _Probe.scanned <= 3 * len(probe), (_Probe.scanned, len(probe))
    # ...and it did not make a call per match (the list of every match is never built).
    assert _Probe.calls <= 25


def test_replace_all_does_no_line_number_work_at_all():
    probe = _probe("x\n" * 40_000)
    new_text, count = plan_edit(_file(probe), "x", "y", replace_all=True)
    assert count == 40_000
    assert new_text == "y\n" * 40_000
    assert _Probe.scanned <= 2 * len(probe)
    assert _Probe.calls <= 3


def test_rejecting_many_matches_does_not_build_a_list_of_them():
    """Memory stays bounded: nothing proportional to the number of matches is kept."""
    text = "x\n" * 1_000_000
    current = _file(text)
    tracemalloc.start()
    try:
        with pytest.raises(EditError):
            plan_edit(current, "x", "y")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024, f"peak {peak} bytes while rejecting an ambiguous edit"


def test_a_large_file_with_millions_of_matches_is_handled_in_one_pass():
    """A ~16 MiB file with 8M occurrences: replaced in full, counted exactly."""
    text = "a\n" * 8_000_000
    new_text, count = plan_edit(_file(text), "a", "b", replace_all=True)
    assert count == 8_000_000
    assert new_text.count("b") == 8_000_000 and "a" not in new_text


def test_line_numbers_in_the_message_are_correct_for_uneven_lines():
    text = "head\nneedle one\nmid\nmid\nneedle two\n\n\nneedle three\n"
    with pytest.raises(EditError) as raised:
        plan_edit(_file(text), "needle", "pin")
    assert "matches 3 places (lines 2, 5, 8)" in str(raised.value)


def test_more_than_ten_matches_are_summarized_with_an_ellipsis():
    text = "".join(f"hit {n}\n" for n in range(25))
    with pytest.raises(EditError) as raised:
        plan_edit(_file(text), "hit", "miss")
    message = str(raised.value)
    assert "matches 25 places" in message
    listed = message.split("(lines ")[1].split(")")[0]
    assert listed == "1, 2, 3, 4, 5, 6, 7, 8, 9, 10…"


def test_a_unique_match_is_still_replaced_once():
    new_text, count = plan_edit(_file("a\nneedle\nb\n"), "needle", "pin")
    assert (new_text, count) == ("a\npin\nb\n", 1)


def test_replace_all_reports_each_replacement():
    new_text, count = plan_edit(_file("a x b x c x\n"), "x", "y", replace_all=True)
    assert (new_text, count) == ("a y b y c y\n", 3)


def test_a_missing_needle_still_explains_itself():
    with pytest.raises(EditError, match="was not found"):
        plan_edit(_file("alpha\nbeta\n"), "gamma", "delta")


def test_overlapping_occurrences_count_like_replace_does():
    # "aa" in "aaaa" is two non-overlapping matches (not three); count and replace agree.
    new_text, count = plan_edit(_file("aaaa\n"), "aa", "b", replace_all=True)
    assert (new_text, count) == ("bb\n", 2)
    with pytest.raises(EditError, match="matches 2 places"):
        plan_edit(_file("aaaa\n"), "aa", "b")


def test_a_crlf_needle_is_found_in_a_crlf_file():
    text = "one\r\ntwo\r\nthree\r\n"
    new_text, count = plan_edit(_file(text, "\r\n", "crlf"), "one\ntwo", "ONE\nTWO")
    assert (new_text, count) == ("ONE\r\nTWO\r\nthree\r\n", 1)


async def test_edit_file_end_to_end_on_a_file_with_many_matches(tmp_path):
    path = tmp_path / "many.txt"
    path.write_text("x\n" * 30_000, encoding="utf-8")
    with pytest.raises(EditError, match="matches 30000 places"):
        await EditFile(workspace=tmp_path).execute(
            file_path="many.txt", old_string="x", new_string="y"
        )
    out = await EditFile(workspace=tmp_path).execute(
        file_path="many.txt", old_string="x", new_string="y", replace_all=True
    )
    assert "30000" in out
    assert path.read_text(encoding="utf-8") == "y\n" * 30_000


# ── #11 ────────────────────────────────────────────────────────────────────────────────────────

CRLF_FILE = b"line1\r\nline2\r\nline3\r\nline4\r\nline5\r\n"


def _changed_rows(rendered: str) -> list[str]:
    """The ``+``/``-`` rows of a rendered diff panel (the panel's box characters removed)."""
    rows = []
    for line in rendered.splitlines():
        text = line.strip("│ ").rstrip()
        if text[:1] in ("+", "-") and not text.startswith(("+++", "---")):
            rows.append(text)
    return rows


def _rendered(diff) -> str:
    console = Console(file=StringIO(), force_terminal=False, width=120)
    DiffPreview(console).render_diff(diff)
    return console.file.getvalue()


def _approval_diff(tmp_path: Path, raw: bytes, old: str, new: str):
    path = tmp_path / "f.txt"
    path.write_bytes(raw)
    planned = EditFile(workspace=tmp_path).preview(
        {"file_path": "f.txt", "old_string": old, "new_string": new}
    )
    assert planned is not None
    return compute_file_diff(*planned)


def test_a_one_line_edit_in_a_crlf_file_shows_one_changed_row(tmp_path):
    diff = _approval_diff(tmp_path, CRLF_FILE, "line3", "LINE-THREE")
    assert diff_stats(diff) == (1, 1)
    rows = _changed_rows(_rendered(diff))
    assert len(rows) == 2, rows  # one removed, one added
    assert any("line3" in r for r in rows) and any("LINE-THREE" in r for r in rows)
    assert not any("line1" in r or "line5" in r for r in rows)


def test_the_hunk_reviewer_sees_a_single_hunk_for_a_crlf_edit(tmp_path):
    diff = _approval_diff(tmp_path, CRLF_FILE, "line3", "LINE-THREE")
    groups = HunkReviewer(Console(file=StringIO())).split_into_hunks(diff)
    assert len(groups) == 1
    changed = [op for op in groups[0] if op[0] != "equal"]
    assert len(changed) == 1  # a single replace, not a rewrite of the whole file


def test_a_crlf_file_stays_crlf_after_the_edit_is_applied(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(CRLF_FILE)
    import asyncio

    asyncio.run(
        EditFile(workspace=tmp_path).execute(
            file_path="f.txt", old_string="line3", new_string="LINE-THREE"
        )
    )
    assert path.read_bytes() == b"line1\r\nline2\r\nLINE-THREE\r\nline4\r\nline5\r\n"


def test_a_multi_line_edit_in_a_crlf_file_shows_only_those_lines(tmp_path):
    diff = _approval_diff(tmp_path, CRLF_FILE, "line2\nline3", "two\nthree\nextra")
    rows = _changed_rows(_rendered(diff))
    assert sorted(r[0] for r in rows) == ["+", "+", "+", "-", "-"]
    assert diff_stats(diff) == (3, 2)


def test_a_mixed_line_ending_file_does_not_show_every_line_as_changed(tmp_path):
    raw = b"a\r\nb\nc\r\nd\ne\r\n"
    diff = _approval_diff(tmp_path, raw, "c", "C")
    assert diff_stats(diff) == (1, 1)
    assert len(_changed_rows(_rendered(diff))) == 2


def test_a_lone_cr_file_is_compared_the_way_it_is_read(tmp_path):
    raw = b"a\rb\rc\r"
    path = tmp_path / "old_mac.txt"
    path.write_bytes(raw)
    diff = compute_file_diff(path, "a\rB\rc\r")
    assert diff_stats(diff) == (1, 1)


def test_an_lf_file_is_unaffected(tmp_path):
    diff = _approval_diff(tmp_path, b"a\nb\nc\n", "b", "B")
    assert diff_stats(diff) == (1, 1)
    assert len(_changed_rows(_rendered(diff))) == 2


def test_the_proposed_text_for_a_new_file_is_shown_exactly(tmp_path):
    diff = compute_file_diff(tmp_path / "new.txt", "a\r\nb\r\n")
    assert diff.is_new_file and diff.proposed == "a\r\nb\r\n"


def test_a_deletion_is_still_a_deletion(tmp_path):
    path = tmp_path / "gone.txt"
    path.write_bytes(CRLF_FILE)
    diff = compute_file_diff(path, "")
    assert diff.is_deletion and diff_stats(diff) == (0, 5)


def test_an_edit_that_changes_nothing_but_endings_shows_no_changes(tmp_path):
    path = tmp_path / "same.txt"
    path.write_bytes(b"a\r\nb\r\n")
    assert diff_stats(compute_file_diff(path, "a\r\nb\r\n")) == (0, 0)
    assert "No changes" in _rendered(compute_file_diff(path, "a\r\nb\r\n"))
