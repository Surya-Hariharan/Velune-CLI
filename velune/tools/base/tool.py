"""Base tool protocol."""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class BaseTool(ABC):
    """Abstract base class for tools."""

    @abstractmethod
    def get_name(self) -> str:
        """Get the tool name."""
        pass

    @abstractmethod
    def get_description(self) -> str:
        """Get the tool description."""
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> Any:
        """Execute the tool."""
        pass

    def get_schema(self) -> Dict[str, Any]:
        """Get the tool's parameter schema."""
        return {}
