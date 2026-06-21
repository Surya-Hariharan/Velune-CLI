"""Abstract base classes and shared types for git provider integrations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class GitProviderError(Exception):
    """Base class for all git provider errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class AuthenticationError(GitProviderError):
    """Token is missing, invalid, or expired (HTTP 401/403)."""


class ResourceNotFoundError(GitProviderError):
    """Repo, issue, or PR does not exist (HTTP 404)."""


class RateLimitError(GitProviderError):
    """API rate limit exceeded (HTTP 429)."""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class PRInfo:
    """A created or retrieved pull request / merge request."""

    number: int
    title: str
    url: str
    state: str  # "open", "closed", "merged"
    head: str  # source branch
    base: str  # target branch
    draft: bool = False
    body: str = ""
    provider: str = ""  # "github" or "gitlab"


@dataclass
class IssueInfo:
    """A GitHub issue or GitLab issue."""

    number: int
    title: str
    body: str
    url: str
    state: str  # "open" or "closed"
    labels: list[str] = field(default_factory=list)
    assignees: list[str] = field(default_factory=list)
    provider: str = ""


@dataclass
class IssueComment:
    """A comment posted on an issue or PR."""

    comment_id: int
    body: str
    url: str
    created_at: str = ""
    author: str = ""


# ---------------------------------------------------------------------------
# Abstract provider
# ---------------------------------------------------------------------------


class BaseGitProvider(ABC):
    """Common interface implemented by GitHub and GitLab providers."""

    # ── Pull requests / Merge requests ──────────────────────────────────────

    @abstractmethod
    async def create_pr(
        self,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
    ) -> PRInfo:
        """Create a pull request (GitHub) or merge request (GitLab).

        Args:
            repo:  "owner/repo" for GitHub; "namespace/project" for GitLab.
            title: PR/MR title.
            head:  Source branch name.
            base:  Target branch name.
            body:  Description markdown body.
            draft: Create as a draft PR/MR if True.

        Returns:
            PRInfo with the newly created PR/MR details.
        """

    @abstractmethod
    async def get_pr(self, repo: str, pr_number: int) -> PRInfo:
        """Fetch details of an existing PR/MR."""

    @abstractmethod
    async def list_prs(
        self,
        repo: str,
        state: str = "open",
        head: str | None = None,
    ) -> list[PRInfo]:
        """List PRs/MRs for a repo, optionally filtered by state or head branch."""

    # ── Issues ──────────────────────────────────────────────────────────────

    @abstractmethod
    async def get_issue(self, repo: str, issue_number: int) -> IssueInfo:
        """Fetch an issue by number."""

    @abstractmethod
    async def comment_on_issue(
        self,
        repo: str,
        issue_number: int,
        body: str,
    ) -> IssueComment:
        """Post a comment on an issue or PR thread.

        Args:
            repo:         "owner/repo" or "namespace/project".
            issue_number: Issue or PR number.
            body:         Markdown comment body.

        Returns:
            IssueComment with the created comment's metadata.
        """

    # ── Push (via git CLI, not HTTP) is handled by GitPushTool ─────────────
