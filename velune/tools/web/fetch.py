"""Web fetch tools."""

from typing import Optional
from velune.tools.base.tool import BaseTool


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
        
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text

    def get_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "URL to fetch",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds",
                },
            },
            "required": ["url"],
        }
