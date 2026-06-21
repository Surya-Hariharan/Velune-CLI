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
        try:
            import git  # gitpython

            repo = git.Repo(str(self._root), search_parent_directories=True)
        except Exception:
            return None

        try:
            branch = repo.active_branch.name
        except TypeError:
            # Detached HEAD
            branch = "HEAD (detached)"

        try:
            head_sha = repo.head.commit.hexsha
        except Exception:
            head_sha = ""

        last_commits: list[dict[str, str]] = []
        try:
            for commit in repo.iter_commits(max_count=self.MAX_COMMITS):
                lines = commit.message.split("\n")
                body = "\n".join(lines[1:]).strip()
                last_commits.append(
                    {
                        "sha7": commit.hexsha[:7],
                        "subject": lines[0][:80],
                        "body": body,
                    }
                )
        except Exception:
            pass

        commit_bodies = [c["body"] for c in last_commits if c.get("body")]

        staged_files: list[str] = []
        modified_files: list[str] = []
        untracked_files: list[str] = []
        try:
            diff_staged = repo.index.diff("HEAD")
            staged_files = [d.a_path for d in diff_staged]
        except Exception:
            pass
        try:
            diff_unstaged = repo.index.diff(None)
            modified_files = [d.a_path for d in diff_unstaged]
        except Exception:
            pass
        try:
            untracked_files = repo.untracked_files[: self.MAX_UNTRACKED]
        except Exception:
            pass

        staged_diff_summary = ""
        try:
            raw_diff = repo.git.diff("--cached")
            if raw_diff:
                if len(raw_diff) > self.MAX_DIFF_CHARS:
                    staged_diff_summary = raw_diff[: self.MAX_DIFF_CHARS] + "\n... [diff truncated]"
                else:
                    staged_diff_summary = raw_diff
        except Exception:
            pass

        last_commit_diff_stat = ""
        try:
            raw_stat = repo.git.diff("HEAD~1", "--stat")
            if raw_stat:
                last_commit_diff_stat = raw_stat[:800]
        except Exception:
            pass

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
