"""A hard limit on a swarm run: wall time, model calls and tokens.

One ``SwarmBudget`` is shared by every agent in a run. Each agent's provider is
wrapped in ``BudgetedProvider``, which *reserves* a call and its ``max_tokens``
before calling the model and settles the real token count afterwards, all under
one lock. Agents running at the same time therefore draw from the same
counters, and a call that would go over the limit is refused before it is made.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from velune.core.types.inference import InferenceRequest, InferenceResponse

DEFAULT_REPLY_TOKENS = 2_048  # reserved for a call that sets no max_tokens


class BudgetExceeded(RuntimeError):
    """A model call was refused because the run's budget is used up."""


@dataclass
class SwarmBudget:
    max_seconds: float = 600.0
    max_calls: int = 40
    max_tokens: int = 200_000
    started: float = field(default_factory=time.monotonic)
    calls: int = 0
    tokens: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def time_left(self) -> float:
        return max(0.0, self.max_seconds - (time.monotonic() - self.started))

    def exhausted_reason(self) -> str:
        """Why no further call can be made, or "" while there is room."""
        if self.time_left() <= 0:
            return f"the time limit ({self.max_seconds:.0f}s) was reached"
        if self.calls >= self.max_calls:
            return f"the model-call limit ({self.max_calls}) was reached"
        if self.tokens >= self.max_tokens:
            return f"the token limit ({self.max_tokens:,}) was reached"
        return ""

    async def reserve(self, request: InferenceRequest) -> int:
        """Claim one call and the request's reply tokens, or raise ``BudgetExceeded``."""
        reserved = request.max_tokens or DEFAULT_REPLY_TOKENS
        async with self._lock:
            reason = self.exhausted_reason()
            if not reason and self.tokens + reserved > self.max_tokens:
                reason = f"the token limit ({self.max_tokens:,}) would be exceeded"
            if reason:
                raise BudgetExceeded(reason)
            self.calls += 1
            self.tokens += reserved
        return reserved

    async def settle(self, reserved: int, used: int) -> None:
        """Replace a reservation with the tokens the call really used."""
        async with self._lock:
            self.tokens += max(0, used) - reserved


class BudgetedProvider:
    """A provider wrapper that charges every call to a shared ``SwarmBudget``.

    Everything else (capabilities, ``SUPPORTS_STREAMING_TOOL_CALLS``, ids) is
    passed through, so the tool loop treats it exactly like the real provider.
    """

    def __init__(self, provider: Any, budget: SwarmBudget) -> None:
        self._provider = provider
        self._budget = budget

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider, name)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        reserved = await self._budget.reserve(request)
        used = 0
        try:
            response = await asyncio.wait_for(
                self._provider.infer(request), timeout=max(1.0, self._budget.time_left())
            )
            used = response.tokens_used or 0
            return response
        finally:
            await self._budget.settle(reserved, used)

    async def stream(self, request: InferenceRequest) -> AsyncIterator[Any]:
        reserved = await self._budget.reserve(request)
        chars = 0
        try:
            async for chunk in self._provider.stream(request):
                chars += len(getattr(chunk, "content", "") or "")
                yield chunk
        finally:
            await self._budget.settle(reserved, chars // 4)
