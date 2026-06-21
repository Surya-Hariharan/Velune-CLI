"""GitLab REST API v4 integration.

Implements BaseGitProvider for GitLab.com and self-hosted instances.

Authentication:
    Pass a personal access token or project access token:
        VELUNE_GITLAB_TOKEN=glpat-...

Self-hosted:
    Override the base URL via:
        VELUNE_GITLAB_URL=https://gitlab.mycompany.com
    or pass base_url to GitLabProvider() directly.

Rate limits:
    Surfaced as RateLimitError on HTTP 429.
"""

from __future__ import annotations

import os
from urllib.parse import quote

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

_GITLAB_COM_API = "https://gitlab.com/api/v4"


class GitLabProvider(BaseGitProvider):
    """GitLab REST API v4 provider (gitlab.com and self-hosted)."""

    def __init__(
        self,
        token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._token = token or os.environ.get("VELUNE_GITLAB_TOKEN", "")
        host = (base_url or os.environ.get("VELUNE_GITLAB_URL", "https://gitlab.com")).rstrip("/")
        self._base_url = f"{host}/api/v4"

    # ------------------------------------------------------------------
    # Merge requests (GitLab's equivalent of PRs)
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
        """Create a GitLab merge request."""
        project_id = self._encode_project(repo)
        mr_title = f"Draft: {title}" if draft else title
        payload = {
            "title": mr_title,
            "source_branch": head,
            "target_branch": base,
            "description": body,
        }
        data = await self._post(f"/projects/{project_id}/merge_requests", payload)
        return self._mr_to_prinfo(data)

    async def get_pr(self, repo: str, pr_number: int) -> PRInfo:
        """Fetch a merge request by IID."""
        project_id = self._encode_project(repo)
        data = await self._get(f"/projects/{project_id}/merge_requests/{pr_number}")
        return self._mr_to_prinfo(data)

    async def list_prs(
        self,
        repo: str,
        state: str = "open",
        head: str | None = None,
    ) -> list[PRInfo]:
        project_id = self._encode_project(repo)
        # GitLab uses "opened" not "open"
        gl_state = "opened" if state == "open" else state
        params: dict = {"state": gl_state, "per_page": 100}
        if head:
            params["source_branch"] = head
        data = await self._get(f"/projects/{project_id}/merge_requests", params=params)
        return [self._mr_to_prinfo(mr) for mr in data]

    # ------------------------------------------------------------------
    # Issues
    # ------------------------------------------------------------------

    async def get_issue(self, repo: str, issue_number: int) -> IssueInfo:
        project_id = self._encode_project(repo)
        data = await self._get(f"/projects/{project_id}/issues/{issue_number}")
        return IssueInfo(
            number=data["iid"],
            title=data["title"],
            body=data.get("description") or "",
            url=data["web_url"],
            state="open" if data["state"] == "opened" else "closed",
            labels=data.get("labels", []),
            assignees=[u["username"] for u in data.get("assignees", [])],
            provider="gitlab",
        )

    async def comment_on_issue(
        self,
        repo: str,
        issue_number: int,
        body: str,
    ) -> IssueComment:
        project_id = self._encode_project(repo)
        data = await self._post(
            f"/projects/{project_id}/issues/{issue_number}/notes",
            {"body": body},
        )
        return IssueComment(
            comment_id=data["id"],
            body=data["body"],
            url=data.get("noteable_url", ""),
            created_at=data.get("created_at", ""),
            author=data.get("author", {}).get("username", ""),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _encode_project(self, repo: str) -> str:
        """URL-encode 'namespace/project' for GitLab path segments."""
        return quote(repo, safe="")

    def _mr_to_prinfo(self, data: dict) -> PRInfo:
        raw_state = data.get("state", "opened")
        state_map = {"opened": "open", "closed": "closed", "merged": "merged"}
        state = state_map.get(raw_state, raw_state)
        title: str = data.get("title", "")
        is_draft = title.startswith(("Draft:", "WIP:"))
        return PRInfo(
            number=data["iid"],
            title=title,
            url=data["web_url"],
            state=state,
            head=data["source_branch"],
            base=data["target_branch"],
            draft=is_draft,
            body=data.get("description") or "",
            provider="gitlab",
        )

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["PRIVATE-TOKEN"] = self._token
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
                "GitLab authentication failed. Check VELUNE_GITLAB_TOKEN.",
                status_code=401,
            )
        if resp.status_code == 403:
            raise AuthenticationError(f"GitLab forbidden: {resp.text[:200]}", status_code=403)
        if resp.status_code == 404:
            raise ResourceNotFoundError(f"GitLab resource not found: {resp.url}", status_code=404)
        if resp.status_code == 409:
            # MR already exists — surface as a descriptive error
            msg = resp.json().get("message", "Conflict")
            raise GitProviderError(f"GitLab conflict: {msg}", status_code=409)
        if resp.status_code == 429:
            raise RateLimitError("GitLab rate limit exceeded.", status_code=429)
        raise GitProviderError(
            f"GitLab API error {resp.status_code}: {resp.text[:200]}",
            status_code=resp.status_code,
        )
