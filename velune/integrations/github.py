"""GitHub REST API v3 integration.

Implements BaseGitProvider for GitHub.com (and future GHES support via
base_url parameter).

Authentication:
    Pass a personal access token (classic or fine-grained) with at least the
    ``repo`` scope:
        VELUNE_GITHUB_TOKEN=ghp_...

Rate limits:
    Authenticated requests: 5,000/hour for classic tokens.
    Fine-grained tokens: varies by permission.
    429 responses are surfaced as RateLimitError.
"""

from __future__ import annotations

import os

import httpx

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

_GITHUB_API = "https://api.github.com"


class GitHubProvider(BaseGitProvider):
    """GitHub REST API v3 provider."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str = _GITHUB_API,
    ) -> None:
        self._token = token or os.environ.get("VELUNE_GITHUB_TOKEN", "")
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Pull requests
    # ------------------------------------------------------------------

    async def create_pr(
        self,
        repo: str,
        title: str,
        head: str,
        base: str,
        body: str = "",
        draft: bool = False,
    ) -> PRInfo:
        payload: dict = {
            "title": title,
            "head": head,
            "base": base,
            "body": body,
            "draft": draft,
        }
        data = await self._post(f"/repos/{repo}/pulls", payload)
        return PRInfo(
            number=data["number"],
            title=data["title"],
            url=data["html_url"],
            state=data["state"],
            head=data["head"]["ref"],
            base=data["base"]["ref"],
            draft=data.get("draft", False),
            body=data.get("body") or "",
            provider="github",
        )

    async def get_pr(self, repo: str, pr_number: int) -> PRInfo:
        data = await self._get(f"/repos/{repo}/pulls/{pr_number}")
        merged = data.get("merged", False)
        state = "merged" if merged else data["state"]
        return PRInfo(
            number=data["number"],
            title=data["title"],
            url=data["html_url"],
            state=state,
            head=data["head"]["ref"],
            base=data["base"]["ref"],
            draft=data.get("draft", False),
            body=data.get("body") or "",
            provider="github",
        )

    async def list_prs(
        self,
        repo: str,
        state: str = "open",
        head: str | None = None,
    ) -> list[PRInfo]:
        params: dict = {"state": state, "per_page": 100}
        if head:
            params["head"] = head
        data = await self._get(f"/repos/{repo}/pulls", params=params)
        return [
            PRInfo(
                number=pr["number"],
                title=pr["title"],
                url=pr["html_url"],
                state=pr["state"],
                head=pr["head"]["ref"],
                base=pr["base"]["ref"],
                draft=pr.get("draft", False),
                body=pr.get("body") or "",
                provider="github",
            )
            for pr in data
        ]

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    async def get_issue(self, repo: str, issue_number: int) -> IssueInfo:
        data = await self._get(f"/repos/{repo}/issues/{issue_number}")
        return IssueInfo(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            url=data["html_url"],
            state=data["state"],
            labels=[lbl["name"] for lbl in data.get("labels", [])],
            assignees=[u["login"] for u in data.get("assignees", [])],
            provider="github",
        )

    async def comment_on_issue(
        self,
        repo: str,
        issue_number: int,
        body: str,
    ) -> IssueComment:
        data = await self._post(
            f"/repos/{repo}/issues/{issue_number}/comments",
            {"body": body},
        )
        return IssueComment(
            comment_id=data["id"],
            body=data["body"],
            url=data["html_url"],
            created_at=data.get("created_at", ""),
            author=data.get("user", {}).get("login", ""),
        )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _get(self, path: str, params: dict | None = None) -> dict | list:
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, headers=self._headers(), params=params)
        return self._handle(resp)

    async def _post(self, path: str, payload: dict) -> dict:
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
        return self._handle(resp)  # type: ignore[return-value]

    def _handle(self, resp: httpx.Response) -> dict | list:
        if resp.status_code in (200, 201):
            return resp.json()
        if resp.status_code == 401:
            raise AuthenticationError(
                "GitHub authentication failed. Check VELUNE_GITHUB_TOKEN.",
                status_code=401,
            )
        if resp.status_code == 403:
            # Could be auth or rate-limit
            msg = resp.json().get("message", "Forbidden")
            if "rate limit" in msg.lower():
                raise RateLimitError(f"GitHub rate limit: {msg}", status_code=403)
            raise AuthenticationError(f"GitHub forbidden: {msg}", status_code=403)
        if resp.status_code == 404:
            raise ResourceNotFoundError(f"GitHub resource not found: {resp.url}", status_code=404)
        if resp.status_code == 422:
            errors = resp.json().get("errors", [])
            raise GitProviderError(f"GitHub validation error: {errors}", status_code=422)
        if resp.status_code == 429:
            raise RateLimitError("GitHub rate limit exceeded.", status_code=429)
        raise GitProviderError(
            f"GitHub API error {resp.status_code}: {resp.text[:200]}",
            status_code=resp.status_code,
        )
