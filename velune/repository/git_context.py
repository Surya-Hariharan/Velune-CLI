"""Git context provider for orchestrator-level context injection.

Gathers current git state (branch, recent commits, staged diff, file lists)
and formats it as a token-efficient block for the Reasoning Council system prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class GitSnapshot:
    """Lightweight git state snapshot captured per orchestration run."""

    branch: str
    head_sha: str
    last_commits: list[dict[str, str]] = field(default_factory=list)  # [{sha7, subject, body}]
    staged_files: list[str] = field(default_factory=list)
    modified_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    staged_diff_summary: str = ""
    commit_bodies: list[str] = field(default_factory=list)
    last_commit_diff_stat: str = ""


class GitContextProvider:
    """Gathers git state from the workspace and formats it for prompt injection."""

    MAX_DIFF_CHARS = 1600  # ~400 tokens at 4 chars/token
    MAX_UNTRACKED = 5
    MAX_COMMITS = 5

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root

    def gather(self) -> GitSnapshot | None:
        """Return a GitSnapshot for the workspace, or None if not a git repo.

        Designed to be called via asyncio.to_thread — fully synchronous.
        """
        import subprocess

        def _git(*args: str) -> str:
            res = subprocess.run(
                ["git", *args],
                cwd=str(self._root),
                capture_output=True,
                text=True,
                check=False,
            )
            return res.stdout.strip() if res.returncode == 0 else ""

        # Check if it's a git repo
        if not _git("rev-parse", "--is-inside-work-tree"):
            return None

        branch = _git("rev-parse", "--abbrev-ref", "HEAD")
        if not branch or branch == "HEAD":
            branch = "HEAD (detached)"

        head_sha = _git("rev-parse", "HEAD")

        last_commits: list[dict[str, str]] = []
        log_out = _git("log", f"-n{self.MAX_COMMITS}", "--format=%h%n%s%n%b%n---VELUNE_COMMIT---")
        if log_out:
            blocks = log_out.split("---VELUNE_COMMIT---")
            for block in blocks:
                lines = block.strip().split("\n")
                if len(lines) >= 2:
                    last_commits.append(
                        {
                            "sha7": lines[0],
                            "subject": lines[1][:80],
                            "body": "\n".join(lines[2:]).strip(),
                        }
                    )

        commit_bodies = [c["body"] for c in last_commits if c.get("body")]

        staged_files = _git("diff", "--name-only", "--cached").splitlines()
        modified_files = _git("diff", "--name-only").splitlines()
        untracked_files = _git("ls-files", "--others", "--exclude-standard").splitlines()[: self.MAX_UNTRACKED]

        staged_diff_summary = ""
        raw_diff = _git("diff", "--cached")
        if raw_diff:
            if len(raw_diff) > self.MAX_DIFF_CHARS:
                staged_diff_summary = raw_diff[: self.MAX_DIFF_CHARS] + "\n... [diff truncated]"
            else:
                staged_diff_summary = raw_diff

        last_commit_diff_stat = ""
        raw_stat = _git("diff", "HEAD~1", "HEAD", "--stat")
        if not raw_stat:
            # Fallback if there is no HEAD~1
            raw_stat = _git("diff", "--stat")
        if raw_stat:
            last_commit_diff_stat = raw_stat[:800]

        return GitSnapshot(
            branch=branch,
            head_sha=head_sha,
            last_commits=last_commits,
            staged_files=staged_files,
            modified_files=modified_files,
            untracked_files=untracked_files,
            staged_diff_summary=staged_diff_summary,
            commit_bodies=commit_bodies,
            last_commit_diff_stat=last_commit_diff_stat,
        )

    def build_context_block(self, snap: GitSnapshot | None) -> str:
        """Format a GitSnapshot into a [GIT CONTEXT] markdown section.

        Returns an empty string if snap is None or there's nothing useful to show.
        """
        if snap is None:
            return ""

        lines: list[str] = ["## GIT CONTEXT"]
        lines.append(f"Branch: `{snap.branch}`")
        if snap.head_sha:
            lines.append(f"HEAD: `{snap.head_sha[:7]}`")

        if snap.last_commits:
            lines.append("\nRecent commits:")
            for c in snap.last_commits:
                lines.append(f"  - `{c['sha7']}` {c['subject']}")
                if c.get("body"):
                    for body_line in c["body"].splitlines()[:3]:
                        lines.append(f"    {body_line[:100]}")

        file_sections: list[str] = []
        if snap.staged_files:
            file_sections.append("Staged: " + ", ".join(snap.staged_files[:10]))
        if snap.modified_files:
            file_sections.append("Modified: " + ", ".join(snap.modified_files[:10]))
        if snap.untracked_files:
            file_sections.append("Untracked: " + ", ".join(snap.untracked_files))
        if file_sections:
            lines.append("")
            lines.extend(file_sections)

        if snap.staged_diff_summary:
            diff = snap.staged_diff_summary
            if len(diff) > self.MAX_DIFF_CHARS:
                diff = diff[: self.MAX_DIFF_CHARS] + "\n... [diff truncated]"
            lines.append("\nStaged diff:")
            lines.append("```diff")
            lines.append(diff)
            lines.append("```")

        if snap.last_commit_diff_stat:
            lines.append("\nLast commit diff stat:")
            lines.append("```")
            lines.append(snap.last_commit_diff_stat)
            lines.append("```")

        # Only return a block if there's something beyond branch/HEAD
        meaningful = (
            snap.staged_files
            or snap.modified_files
            or snap.untracked_files
            or snap.staged_diff_summary
            or snap.last_commits
        )
        if not meaningful:
            return ""

        return "\n".join(lines)
