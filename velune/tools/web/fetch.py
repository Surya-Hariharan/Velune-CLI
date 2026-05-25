"""Web fetch tools."""

from velune.tools.base.tool import BaseTool
from velune.tools.web.validator import validate_url


class WebFetch(BaseTool):
    """Tool for fetching web content."""

    def get_name(self) -> str:
        return "web_fetch"

    def get_description(self) -> str:
        return "Fetch content from a URL"

    async def execute(
        self,
        url: str,
        timeout: int = 10,
    ) -> str:
        """Fetch content from URL."""
        import httpx

        is_valid, error = validate_url(url)
        if not is_valid:
            raise ValueError(f"URL validation failed: {error}")

        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            max_redirects=3,  # Prevent redirect chains to internal services
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            # Limit response size to prevent memory exhaustion
            content = response.text
            if len(content) > 500_000:  # 500KB limit
                content = content[:500_000] + "\n... [TRUNCATED: response exceeded 500KB]"
            return content

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
