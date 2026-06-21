"""Tests for HunkReviewer — hunk splitting and reconstruction."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from velune.execution.diff_preview import FileDiff
from velune.execution.hunk_review import HunkDecision, HunkResult, HunkReviewer


def _make_diff(original: str, proposed: str, path: str = "test.py") -> FileDiff:
    return FileDiff(
        path=Path(path),
        original=original,
        proposed=proposed,
        is_new_file=False,
        is_deletion=False,
    )


def _make_reviewer() -> HunkReviewer:
    return HunkReviewer(console=MagicMock())


class TestHunkSplitting:
    def test_identical_content_yields_no_hunks(self):
        reviewer = _make_reviewer()
        diff = _make_diff("a\nb\nc\n", "a\nb\nc\n")
        hunks = reviewer.split_into_hunks(diff)
        assert hunks == []

    def test_single_change_is_one_hunk(self):
        reviewer = _make_reviewer()
        diff = _make_diff("a\nb\nc\n", "a\nB\nc\n")
        hunks = reviewer.split_into_hunks(diff)
        assert len(hunks) == 1

    def test_two_distant_changes_split_into_two_hunks(self):
        reviewer = _make_reviewer()
        # Changes separated by more than 2*CONTEXT_LINES unchanged lines
        lines_a = ["line1\n"] + [f"unchanged{i}\n" for i in range(10)] + ["lineZ\n"]
        lines_b = ["LINE1\n"] + [f"unchanged{i}\n" for i in range(10)] + ["LINEZ\n"]
        diff = _make_diff("".join(lines_a), "".join(lines_b))
        hunks = reviewer.split_into_hunks(diff)
        assert len(hunks) == 2

    def test_new_file_gives_single_hunk(self):
        reviewer = _make_reviewer()
        diff = FileDiff(
            path=Path("new.py"),
            original="",
            proposed="x = 1\n",
            is_new_file=True,
            is_deletion=False,
        )
        hunks = reviewer.split_into_hunks(diff)
        assert len(hunks) == 1


class TestHunkReconstruct:
    def test_accept_all_hunks_equals_proposed(self):
        reviewer = _make_reviewer()
        diff = _make_diff("a\nb\nc\n", "a\nB\nc\n")
        hunks = reviewer.split_into_hunks(diff)
        results = [HunkResult(i, HunkDecision.ACCEPT, g) for i, g in enumerate(hunks)]
        result = reviewer._reconstruct(diff, hunks, results)
        assert result == "a\nB\nc\n"

    def test_reject_all_hunks_equals_original(self):
        reviewer = _make_reviewer()
        diff = _make_diff("a\nb\nc\n", "a\nB\nc\n")
        hunks = reviewer.split_into_hunks(diff)
        results = [HunkResult(i, HunkDecision.REJECT, g) for i, g in enumerate(hunks)]
        result = reviewer._reconstruct(diff, hunks, results)
        assert result == "a\nb\nc\n"

    def test_accept_first_reject_second(self):
        reviewer = _make_reviewer()
        lines_a = ["first\n"] + [f"mid{i}\n" for i in range(10)] + ["last\n"]
        lines_b = ["FIRST\n"] + [f"mid{i}\n" for i in range(10)] + ["LAST\n"]
        diff = _make_diff("".join(lines_a), "".join(lines_b))
        hunks = reviewer.split_into_hunks(diff)
        assert len(hunks) == 2

        results = [
            HunkResult(0, HunkDecision.ACCEPT, hunks[0]),
            HunkResult(1, HunkDecision.REJECT, hunks[1]),
        ]
        result = reviewer._reconstruct(diff, hunks, results)
        result_lines = result.splitlines()
        assert result_lines[0] == "FIRST"   # accepted
        assert result_lines[-1] == "last"   # rejected (original)

    def test_no_change_regions_preserved(self):
        reviewer = _make_reviewer()
        diff = _make_diff("keep\nchange\nkeep2\n", "keep\nCHANGE\nkeep2\n")
        hunks = reviewer.split_into_hunks(diff)
        results = [HunkResult(0, HunkDecision.ACCEPT, hunks[0])]
        result = reviewer._reconstruct(diff, hunks, results)
        assert "keep\n" in result
        assert "CHANGE\n" in result
        assert "keep2\n" in result


class TestReviewHunksAutoAccept:
    @pytest.mark.asyncio
    async def test_auto_accept_returns_proposed(self):
        reviewer = _make_reviewer()
        diff = _make_diff("a\nb\n", "a\nB\n")
        result = await reviewer.review_hunks(diff, auto_accept=True)
        assert result == "a\nB\n"

    @pytest.mark.asyncio
    async def test_no_hunks_returns_proposed(self):
        reviewer = _make_reviewer()
        diff = _make_diff("a\n", "a\n")
        result = await reviewer.review_hunks(diff, auto_accept=True)
        assert result == "a\n"
