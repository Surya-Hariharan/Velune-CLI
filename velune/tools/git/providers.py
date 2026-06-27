"""Git provider tools: push, PR creation, issue fetching, issue commenting.

These tools bridge the local git repo with remote providers (GitHub / GitLab).
They are intentionally thin wrappers — the real logic lives in velune.integrations.

Environment variables:
    VELUNE_GITHUB_TOKEN   — GitHub personal access token
    VELUNE_GITLAB_TOKEN   — GitLab personal access token
    VELUNE_GITLAB_URL     — GitLab base URL (default: https://gitlab.com)
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from urllib.parse import urlparse as _urlparse

from velune.integrations.base import GitProviderError, IssueInfo, PRInfo
from velune.tools.base.tool import BaseTool, ToolPermission

_log = logging.getLogger("velune.tools.git.providers")


# ---------------------------------------------------------------------------
# Helper: strict hostname extraction from git remote URLs
# ---------------------------------------------------------------------------


def _remote_hostname(url: str) -> str:
    """Return the lower-case hostname from a git remote URL.

    Handles both HTTPS (https://github.com/owner/repo) and SSH
    (git@github.com:owner/repo) forms. Returns an empty string on any
    parse failure so callers can safely compare without crashing.

    This avoids the substring-in-URL anti-pattern (CWE-184) where a crafted
    path like ``https://evil.com/redirect-to/github.com/foo`` would satisfy
    ``"github.com" in url`` but resolve to the wrong host.
    """
    normalized = url
    if normalized.startswith("git@"):
        # git@host:path → https://host/path so urlparse can extract hostname
        normalized = "https://" + normalized[4:].replace(":", "/", 1)
    try:
        return (_urlparse(normalized).hostname or "").lower()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Helper: detect provider from remote URL
# ---------------------------------------------------------------------------


def _git_run(cwd: Path, *args: str) -> str:
    import subprocess

    res = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Git error: {res.stderr.strip() or res.stdout.strip()}")
    return res.stdout.strip()


def _detect_provider(workspace: Path) -> tuple[str, str]:
    """Infer (provider_name, repo_slug) from the origin remote URL.

    Returns:
        ("github", "owner/repo") or ("gitlab", "namespace/project")

    Raises:
        ValueError if no suitable remote is found.
    """
    try:
        url = _git_run(workspace, "remote", "get-url", "origin")
    except Exception:
        try:
            out = _git_run(workspace, "remote")
            if out:
                first_remote = out.splitlines()[0]
                url = _git_run(workspace, "remote", "get-url", first_remote)
            else:
                raise ValueError("No remotes found")
        except Exception as exc:
            raise ValueError(f"Cannot detect git remote: {exc}") from exc

    url = url.rstrip("/").removesuffix(".git")

    host = _remote_hostname(url)

    if host == "github.com":
        # Extract owner/repo slug after the hostname for both HTTPS and SSH forms.
        slug = url.split("github.com")[-1].lstrip("/").lstrip(":")
        return "github", slug

    # Resolve the expected GitLab hostname from the env override (if set) or
    # fall back to the public gitlab.com. Compare by hostname only — never by
    # substring — to prevent path-based bypass (CWE-184 / CodeQL incomplete-url-sanitization).
    gitlab_base = os.environ.get("VELUNE_GITLAB_URL", "https://gitlab.com")
    gitlab_host = _remote_hostname(gitlab_base) or "gitlab.com"
    if host == gitlab_host:
        slug = url.split(".com")[-1].lstrip("/") if ".com" in url else url.split(":")[-1]
        return "gitlab", slug

    raise ValueError(
        f"Could not detect provider from remote URL: {url}. "
        "Set VELUNE_GITHUB_TOKEN or VELUNE_GITLAB_TOKEN explicitly."
    )


def _get_current_branch(workspace: Path) -> str:
    try:
        return _git_run(workspace, "symbolic-ref", "--short", "HEAD")
    except Exception as exc:
        raise ValueError(f"Cannot determine current branch: {exc}") from exc


def _get_provider(workspace: Path, provider_override: str | None = None):
    """Return a configured provider instance."""
    from velune.integrations import get_provider

    if provider_override:
        name = provider_override.lower()
        _, repo = _detect_provider(workspace)
    else:
        name, repo = _detect_provider(workspace)

    token_env = "VELUNE_GITHUB_TOKEN" if name == "github" else "VELUNE_GITLAB_TOKEN"
    token = os.environ.get(token_env, "")
    if not token:
        raise GitProviderError(f"No token found. Set {token_env} in your environment.")

    return get_provider(name, token=token), repo


# ---------------------------------------------------------------------------
# GitPushTool — push current branch to remote
# ---------------------------------------------------------------------------


class GitPushTool(BaseTool):
    """Push the current branch (or a specified branch) to the remote."""

    HOOK_TOOL_NAME = "GitPush"

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "git_push"

    def get_description(self) -> str:
        return "Push the current branch to the remote (origin)"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.GIT_WRITE, ToolPermission.NETWORK_ACCESS}

    async def execute(
        self,
        branch: str | None = None,
        remote: str = "origin",
        set_upstream: bool = True,
        force: bool = False,
    ) -> str:
        """Push to remote.

        Args:
            branch:       Branch to push. Defaults to the current active branch.
            remote:       Remote name (default: "origin").
            set_upstream: If True, sets tracking reference (-u flag).
            force:        Force-push (use with caution).

        Returns:
            Human-readable status string.
        """

        def _push() -> str:
            target_branch = branch
            if not target_branch:
                try:
                    target_branch = _git_run(self.workspace, "symbolic-ref", "--short", "HEAD")
                except Exception:
                    raise ValueError("Cannot determine current branch for push")

            if target_branch.startswith("-"):
                raise ValueError(f"Invalid branch name: {target_branch!r}")

            push_args = [remote, f"{target_branch}:{target_branch}"]
            if set_upstream:
                push_args.insert(0, "-u")
            if force:
                push_args.insert(0, "--force")

            try:
                _git_run(self.workspace, "push", *push_args)
            except RuntimeError as e:
                raise RuntimeError(f"Push failed: {e}")

            return f"Pushed {target_branch!r} → {remote}"

        return await asyncio.to_thread(_push)

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "branch": {
                    "type": "string",
                    "description": "Branch name (default: current branch)",
                },
                "remote": {"type": "string", "description": "Remote name (default: origin)"},
                "set_upstream": {"type": "boolean", "description": "Set upstream tracking (-u)"},
                "force": {"type": "boolean", "description": "Force push (dangerous)"},
            },
        }


# ---------------------------------------------------------------------------
# CreatePRTool — create PR on GitHub or GitLab
# ---------------------------------------------------------------------------


class CreatePRTool(BaseTool):
    """Create a pull request (GitHub) or merge request (GitLab) for the current branch."""

    HOOK_TOOL_NAME = "CreatePR"

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "create_pr"

    def get_description(self) -> str:
        return "Create a pull/merge request on GitHub or GitLab for the current or specified branch"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.GIT_READ, ToolPermission.NETWORK_ACCESS}

    async def execute(
        self,
        title: str,
        body: str = "",
        base: str = "main",
        head: str | None = None,
        draft: bool = False,
        provider: str | None = None,
    ) -> dict:
        """Create a PR/MR.

        Args:
            title:    PR/MR title.
            body:     Description body (Markdown).
            base:     Target branch (default: main).
            head:     Source branch (default: current branch).
            draft:    Create as draft if True.
            provider: "github" or "gitlab". Auto-detected if not set.

        Returns:
            Dict with pr_number, url, state, title keys.
        """
        if not head:
            head = await asyncio.to_thread(_get_current_branch, self.workspace)

        git_provider, repo = await asyncio.to_thread(_get_provider, self.workspace, provider)

        pr: PRInfo = await git_provider.create_pr(
            repo=repo,
            title=title,
            head=head,
            base=base,
            body=body,
            draft=draft,
        )
        _log.info("Created PR #%s: %s", pr.number, pr.url)
        return {
            "pr_number": pr.number,
            "url": pr.url,
            "state": pr.state,
            "title": pr.title,
            "draft": pr.draft,
            "provider": pr.provider,
        }

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "PR/MR title"},
                "body": {"type": "string", "description": "Description (Markdown)"},
                "base": {"type": "string", "description": "Target branch (default: main)"},
                "head": {
                    "type": "string",
                    "description": "Source branch (default: current branch)",
                },
                "draft": {"type": "boolean", "description": "Create as draft"},
                "provider": {
                    "type": "string",
                    "enum": ["github", "gitlab"],
                    "description": "Git provider",
                },
            },
            "required": ["title"],
        }


# ---------------------------------------------------------------------------
# GetIssueTool — fetch issue body to inject as context
# ---------------------------------------------------------------------------


class GetIssueTool(BaseTool):
    """Fetch a GitHub/GitLab issue and return its title + body as context."""

    HOOK_TOOL_NAME = "GetIssue"

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "get_issue"

    def get_description(self) -> str:
        return "Fetch a GitHub or GitLab issue by number and return its content"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.NETWORK_ACCESS}

    async def execute(
        self,
        issue_number: int,
        provider: str | None = None,
    ) -> dict:
        """Fetch issue content.

        Args:
            issue_number: The issue number.
            provider:     "github" or "gitlab". Auto-detected if not set.

        Returns:
            Dict with number, title, body, url, state, labels keys.
        """
        git_provider, repo = await asyncio.to_thread(_get_provider, self.workspace, provider)
        issue: IssueInfo = await git_provider.get_issue(repo=repo, issue_number=issue_number)
        return {
            "number": issue.number,
            "title": issue.title,
            "body": issue.body,
            "url": issue.url,
            "state": issue.state,
            "labels": issue.labels,
            "assignees": issue.assignees,
            "provider": issue.provider,
        }

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "Issue number"},
                "provider": {"type": "string", "enum": ["github", "gitlab"]},
            },
            "required": ["issue_number"],
        }


# ---------------------------------------------------------------------------
# CommentIssueTool — post a comment on an issue or PR thread
# ---------------------------------------------------------------------------


class CommentIssueTool(BaseTool):
    """Post a comment on a GitHub issue / PR or GitLab issue / MR."""

    HOOK_TOOL_NAME = "CommentIssue"

    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve() if workspace else Path.cwd().resolve()

    def get_name(self) -> str:
        return "comment_issue"

    def get_description(self) -> str:
        return "Post a comment on a GitHub/GitLab issue or pull request"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.NETWORK_ACCESS}

    async def execute(
        self,
        issue_number: int,
        body: str,
        provider: str | None = None,
    ) -> dict:
        """Post a comment.

        Args:
            issue_number: Issue or PR number to comment on.
            body:         Markdown comment body.
            provider:     "github" or "gitlab". Auto-detected if not set.

        Returns:
            Dict with comment_id, url, author, created_at.
        """
        git_provider, repo = await asyncio.to_thread(_get_provider, self.workspace, provider)
        comment = await git_provider.comment_on_issue(
            repo=repo,
            issue_number=issue_number,
            body=body,
        )
        return {
            "comment_id": comment.comment_id,
            "url": comment.url,
            "author": comment.author,
            "created_at": comment.created_at,
        }

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "issue_number": {"type": "integer", "description": "Issue or PR number"},
                "body": {"type": "string", "description": "Comment body (Markdown)"},
                "provider": {"type": "string", "enum": ["github", "gitlab"]},
            },
            "required": ["issue_number", "body"],
        }
