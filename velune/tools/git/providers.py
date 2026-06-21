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

from velune.integrations.base import GitProviderError, IssueInfo, PRInfo
from velune.tools.base.tool import BaseTool, ToolPermission

_log = logging.getLogger("velune.tools.git.providers")


# ---------------------------------------------------------------------------
# Helper: detect provider from remote URL
# ---------------------------------------------------------------------------


def _detect_provider(workspace: Path) -> tuple[str, str]:
    """Infer (provider_name, repo_slug) from the origin remote URL.

    Returns:
        ("github", "owner/repo") or ("gitlab", "namespace/project")

    Raises:
        ValueError if no suitable remote is found.
    """
    try:
        import git

        repo = git.Repo(str(workspace), search_parent_directories=True)
        remotes = {r.name: r.url for r in repo.remotes}
        url: str = remotes.get("origin") or next(iter(remotes.values()))
    except Exception as exc:
        raise ValueError(f"Cannot detect git remote: {exc}") from exc

    url = url.rstrip("/").removesuffix(".git")

    if "github.com" in url:
        # ssh: git@github.com:owner/repo  or  https://github.com/owner/repo
        slug = url.split("github.com")[-1].lstrip("/").lstrip(":")
        return "github", slug

    if "gitlab" in url or os.environ.get("VELUNE_GITLAB_URL", ""):
        slug = url.split(".com")[-1].lstrip("/") if ".com" in url else url.split(":")[-1]
        return "gitlab", slug

    raise ValueError(
        f"Could not detect provider from remote URL: {url}. "
        "Set VELUNE_GITHUB_TOKEN or VELUNE_GITLAB_TOKEN explicitly."
    )


def _get_current_branch(workspace: Path) -> str:
    try:
        import git

        repo = git.Repo(str(workspace), search_parent_directories=True)
        return repo.active_branch.name
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
            try:
                import git
            except ImportError as e:
                raise RuntimeError("gitpython is required: pip install gitpython") from e

            repo = git.Repo(str(self.workspace), search_parent_directories=True)
            target_branch = branch or repo.active_branch.name

            if target_branch.startswith("-"):
                raise ValueError(f"Invalid branch name: {target_branch!r}")

            push_args: dict = {}
            if set_upstream:
                push_args["set_upstream"] = True
            if force:
                push_args["force"] = True

            remote_obj = repo.remote(remote)
            result = remote_obj.push(f"{target_branch}:{target_branch}", **push_args)

            if not result:
                return f"Pushed {target_branch!r} → {remote}"

            summaries = []
            for info in result:
                flag = getattr(info, "flags", 0)
                # PushInfo.ERROR = 1024
                if flag & 1024:
                    raise RuntimeError(f"Push failed: {getattr(info, 'summary', 'unknown error')}")
                summaries.append(getattr(info, "summary", "ok").strip())

            return f"Pushed {target_branch!r} → {remote}: {', '.join(summaries)}"

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
