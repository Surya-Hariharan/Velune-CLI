"""Contracts that define the orchestration boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol


class OrchestrationEngine(Protocol):
    """Future multi-agent orchestration engine contract."""

    async def plan(self, prompt: str) -> dict[str, object]:
        """Produce an execution plan for the prompt."""
        ...

    async def execute(self, prompt: str) -> dict[str, object]:
        """Execute the prompt through the selected workflow."""
        ...

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        """Stream intermediate orchestration events."""
        ...


OrchestrationFactory = Callable[[], Awaitable[OrchestrationEngine]]