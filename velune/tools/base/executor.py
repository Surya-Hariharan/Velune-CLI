"""Permissioned tool execution coordinator with retries and tracing."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Optional

from pydantic import BaseModel, Field

from velune.tools.base.registry import ToolRegistry
from velune.tools.base.tool import ToolCallContext, ToolPermission


class ToolExecutionResult(BaseModel):
    """Structured output envelope for tool calls."""

    tool_name: str
    success: bool
    output: Any = None
    error: Optional[str] = None
    attempts: int = 1
    duration_ms: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    trace: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionCoordinator:
    """Runs registered tools with policy checks and retry boundaries."""

    def __init__(
        self,
        tool_registry: ToolRegistry,
        default_timeout: float = 30.0,
        default_retries: int = 1,
    ) -> None:
        self.tool_registry = tool_registry
        self.default_timeout = default_timeout
        self.default_retries = default_retries

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        run_id: str,
        actor: str,
        timeout: Optional[float] = None,
        retries: Optional[int] = None,
        granted_permissions: Optional[set[ToolPermission]] = None,
    ) -> ToolExecutionResult:
        """Execute a tool with retries and structured tracing."""

        tool = self.tool_registry.get(tool_name)
        if tool is None:
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                error="tool_not_found",
                trace={"run_id": run_id, "actor": actor},
            )

        context = ToolCallContext(
            run_id=run_id,
            actor=actor,
            permissions=granted_permissions or set(ToolPermission),
        )

        missing_permissions = set(tool.get_required_permissions()) - context.permissions
        if missing_permissions:
            return ToolExecutionResult(
                tool_name=tool_name,
                success=False,
                error=f"missing_permissions:{sorted(permission.value for permission in missing_permissions)}",
                trace={"run_id": run_id, "actor": actor},
            )

        tool.validate_input(arguments)

        max_attempts = max(1, (retries if retries is not None else self.default_retries) + 1)
        run_timeout = timeout if timeout is not None else self.default_timeout

        start = time.perf_counter()
        last_error: Optional[str] = None

        for attempt in range(1, max_attempts + 1):
            try:
                result = await asyncio.wait_for(tool.execute(**arguments), timeout=run_timeout)
                duration_ms = (time.perf_counter() - start) * 1000
                return ToolExecutionResult(
                    tool_name=tool_name,
                    success=True,
                    output=result,
                    attempts=attempt,
                    duration_ms=duration_ms,
                    trace={
                        "run_id": run_id,
                        "actor": actor,
                        "timeout_s": run_timeout,
                    },
                )
            except Exception as exc:  # noqa: BLE001 - tool boundaries intentionally catch failures.
                last_error = str(exc)

        duration_ms = (time.perf_counter() - start) * 1000
        return ToolExecutionResult(
            tool_name=tool_name,
            success=False,
            error=last_error or "unknown_tool_error",
            attempts=max_attempts,
            duration_ms=duration_ms,
            trace={
                "run_id": run_id,
                "actor": actor,
                "timeout_s": run_timeout,
            },
        )
