"""Web fetch tools."""

from velune.tools.base.tool import BaseTool, ToolPermission
from velune.tools.web.validator import validate_url


class WebFetch(BaseTool):
    """Tool for fetching web content."""

    def get_name(self) -> str:
        return "web_fetch"

    def get_required_permissions(self) -> set[ToolPermission]:
        return {ToolPermission.NETWORK_ACCESS}

    def get_description(self) -> str:
        return "Fetch content from a URL"

    async def execute(
        self,
        url: str,
        timeout: int = 10,
    ) -> str:
        """Fetch content from URL.

        Redirects are followed manually rather than by httpx so that every hop
        is re-validated: capping the redirect *count* does not restrict the
        *destination*, so a permitted public host could otherwise 302 the
        request to an internal service (e.g. the cloud metadata endpoint).
        """
        from urllib.parse import urljoin

        import httpx

        max_redirects = 3
        current = url

        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(max_redirects + 1):
                is_valid, error = validate_url(current)
                if not is_valid:
                    raise ValueError(f"URL validation failed: {error}")

                response = await client.get(current)
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        break
                    # Resolve relative redirects, then loop to re-validate the
                    # destination before the next request is issued.
                    current = urljoin(current, location)
                    continue

                response.raise_for_status()
                # Limit response size to prevent memory exhaustion
                content = response.text
                if len(content) > 500_000:  # 500KB limit
                    content = content[:500_000] + "\n... [TRUNCATED: response exceeded 500KB]"
                return content

        raise ValueError(f"URL validation failed: too many redirects (>{max_redirects})")

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Fetch content from a URL. HTTPS only. Private/internal IPs blocked.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds",
                },
            },
            "required": ["url"],
        }
