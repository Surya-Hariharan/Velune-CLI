"""Semantic commit message generator for Velune auto-commits."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class CommitMessageGenerator:
    """Generates semantic commit messages from changed files and task context."""

    _TYPE_RULES: list[tuple[str, str]] = [
        (r"(^|[\s/\\])test_|_test\.py|\.test\.[jt]sx?|\.spec\.[jt]sx?", "test"),
        (r"\.(md|rst|txt)\b", "docs"),
        (r"\bsetup\.(py|cfg)\b|\bpyproject\.toml\b|\bpackage\.json\b", "chore"),
        (r"(docker|compose|\.github|\.ci)", "chore"),
        (r"(fix|bug|patch|hotfix)", "fix"),
        (r"(refactor|cleanup|clean_up|rename|reorganize)", "refactor"),
    ]

    def generate(self, paths: list[Path], task: str, workspace: Path) -> str:
        change_type = self._classify_change_type(paths)
        sentence = self._extract_sentence(task)
        return f"velune({change_type}): {sentence}"

    def _classify_change_type(self, paths: list[Path]) -> str:
        names = " ".join(p.name for p in paths)
        full = " ".join(str(p) for p in paths)
        combined = f"{names} {full}".lower()

        for pattern, label in self._TYPE_RULES:
            if re.search(pattern, combined, re.IGNORECASE):
                return label

        return "feat"

    def _extract_sentence(self, task: str) -> str:
        task = task.strip()
        # Take up to the first sentence-ending punctuation
        match = re.search(r"[.!?\n]", task)
        sentence = task[: match.start()].strip() if match else task
        # Truncate to 60 chars
        if len(sentence) > 60:
            sentence = sentence[:57] + "..."
        return sentence or task[:60]

    def _compute_diff_stats(self, workspace: Path) -> dict:
        """Run git diff --numstat HEAD~1 and return added/removed totals."""
        try:
            result = subprocess.run(
                ["git", "diff", "--numstat", "HEAD~1"],
                cwd=workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            added = removed = files = 0
            for line in result.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 2:
                    try:
                        added += int(parts[0])
                        removed += int(parts[1])
                        files += 1
                    except ValueError:
                        pass
            return {"added": added, "removed": removed, "files_changed": files}
        except Exception:
            return {"added": 0, "removed": 0, "files_changed": 0}
