"""Git provider integrations — GitHub, GitLab.

Usage:
    from velune.integrations import get_provider
    provider = get_provider("github", token="ghp_...")
    pr = await provider.create_pr(repo="owner/repo", title="...", head="feat/x", base="main")
"""

from __future__ import annotations

from velune.integrations.base import (
    AuthenticationError,
    BaseGitProvider,
    GitProviderError,
    IssueComment,
    IssueInfo,
    PRInfo,
    RateLimitError,
    ResourceNotFoundError,
)


def get_provider(provider: str, token: str, base_url: str | None = None) -> BaseGitProvider:
    """Return a configured provider instance by name.

    Args:
        provider: "github" or "gitlab"
        token:    Personal access token / OAuth token
        base_url: Optional self-hosted base URL (GitLab only)

    Raises:
        ValueError: If provider name is unknown.
    """
    p = provider.lower()
    if p == "github":
        from velune.integrations.github import GitHubProvider

        return GitHubProvider(token=token)
    if p == "gitlab":
        from velune.integrations.gitlab import GitLabProvider

        return GitLabProvider(token=token, base_url=base_url)
    raise ValueError(f"Unknown git provider: '{provider}'. Supported: github, gitlab")


__all__ = [
    "get_provider",
    "BaseGitProvider",
    "GitProviderError",
    "AuthenticationError",
    "RateLimitError",
    "ResourceNotFoundError",
    "PRInfo",
    "IssueInfo",
    "IssueComment",
]
