"""Tests for CommitMessageGenerator."""

from __future__ import annotations

from pathlib import Path

import pytest

from velune.repository.commit_message import CommitMessageGenerator


class TestCommitMessageGenerator:
    def setup_method(self):
        self.gen = CommitMessageGenerator()
        self.workspace = Path("/workspace")

    def test_output_starts_with_velune_prefix(self):
        msg = self.gen.generate([Path("foo.py")], "add retry logic", self.workspace)
        assert msg.startswith("velune(")

    def test_short_task_used_verbatim(self):
        msg = self.gen.generate([Path("foo.py")], "add retry logic", self.workspace)
        assert "add retry logic" in msg

    def test_long_task_truncated(self):
        long_task = "a" * 100
        msg = self.gen.generate([Path("foo.py")], long_task, self.workspace)
        # Subject part (after "velune(feat): ") should not exceed 60 chars
        subject = msg.split(": ", 1)[1]
        assert len(subject) <= 63  # 60 + "..."

    def test_long_task_truncated_at_sentence_boundary(self):
        task = "Fix the retry loop. Also fix the other thing that was broken."
        msg = self.gen.generate([Path("foo.py")], task, self.workspace)
        assert "Fix the retry loop" in msg
        # Should not include the second sentence
        assert "Also fix" not in msg

    def test_test_files_classified_as_test(self):
        msg = self.gen.generate([Path("test_foo.py")], "add tests", self.workspace)
        assert "velune(test):" in msg

    def test_spec_files_classified_as_test(self):
        msg = self.gen.generate([Path("foo.spec.ts")], "add tests", self.workspace)
        assert "velune(test):" in msg

    def test_md_files_classified_as_docs(self):
        msg = self.gen.generate([Path("README.md")], "update docs", self.workspace)
        assert "velune(docs):" in msg

    def test_rst_files_classified_as_docs(self):
        msg = self.gen.generate([Path("CHANGELOG.rst")], "update changelog", self.workspace)
        assert "velune(docs):" in msg

    def test_setup_py_classified_as_chore(self):
        msg = self.gen.generate([Path("setup.py")], "bump version", self.workspace)
        assert "velune(chore):" in msg

    def test_regular_py_classified_as_feat(self):
        msg = self.gen.generate([Path("velune/core/runtime.py")], "add feature", self.workspace)
        assert "velune(feat):" in msg

    def test_multiple_paths_uses_first_match(self):
        paths = [Path("test_foo.py"), Path("README.md")]
        msg = self.gen.generate(paths, "update tests and docs", self.workspace)
        # test pattern should match first
        assert "velune(test):" in msg

    def test_empty_task_handled(self):
        msg = self.gen.generate([Path("foo.py")], "", self.workspace)
        assert msg.startswith("velune(")

    def test_task_with_newline_uses_first_line(self):
        task = "Add feature\n\nMore details here."
        msg = self.gen.generate([Path("foo.py")], task, self.workspace)
        assert "Add feature" in msg
        assert "More details" not in msg
