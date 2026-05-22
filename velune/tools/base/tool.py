"""Base tool protocol and execution contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ToolPermission(str, Enum):
    """Permission boundaries enforced at tool execution time."""

    FILESYSTEM_READ = "filesystem.read"
    FILESYSTEM_WRITE = "filesystem.write"
    GIT_READ = "git.read"
    GIT_WRITE = "git.write"
    TERMINAL_EXECUTE = "terminal.execute"
    NETWORK_ACCESS = "network.access"


@dataclass(slots=True)
class ToolCallContext:
    """Execution context passed into policy-aware tool calls."""

    run_id: str
    actor: str
    workspace: Path | None = None
    permissions: set[ToolPermission] = field(default_factory=set)


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

    def get_schema(self) -> dict[str, Any]:
        """Get the tool's parameter schema."""
        return {}

    def get_required_permissions(self) -> set[ToolPermission]:
        """Permissions required to execute this tool."""

        return set()

    def validate_input(self, payload: dict[str, Any]) -> None:
        """Validate tool input before execution."""

        del payload
