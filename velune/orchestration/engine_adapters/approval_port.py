"""A human-facing approval port for the native acting engine.

This is a presentation layer around the engine's approval core, not a new authority. The port shows a
person one specific invocation and relays a yes/no. The engine's ``ApprovalLedger`` still does the
real work: it binds the decision to the tool, the call digest and the invocation id, spends it once,
and expires it.

* The decision is built here from the invocation the port was handed, never from anything the
  prompt returns, so a UI cannot produce a mismatched or foreign approval.
* Only a literal ``True`` approves. ``False``, ``None``, back, cancel, an error, a timeout and a
  missing prompt all mean no.
* There is no "always allow", no session grant and no remembered answer.
* Cancellation is never swallowed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from velune.orchestration.engine.approval import (
    DEFAULT_APPROVAL_TTL_S,
    ApprovalDecision,
    InvocationRequest,
)

DEFAULT_PROMPT_TIMEOUT_S = 60.0
DIGEST_PREFIX_CHARS = 12


@dataclass(frozen=True)
class ApprovalPrompt:
    """What a UI shows. Plain text only: a UI must render it as text, never as markup."""

    title: str
    detail: str
    hint: str


AskCallable = Callable[[ApprovalPrompt], Awaitable[Any]]


def build_prompt(invocation: InvocationRequest) -> ApprovalPrompt:
    """Describe exactly one invocation. Contains no model-written prose."""
    digest = invocation.call_digest[:DIGEST_PREFIX_CHARS]
    return ApprovalPrompt(
        title=f"Run read-only tool '{invocation.tool}'?",
        detail=invocation.display or "(no arguments shown)",
        hint=(
            f"Approves this single call only (id {invocation.invocation_id[:8]}, "
            f"digest {digest}). It expires and cannot be reused."
        ),
    )


class InteractiveApprovalPort:
    """Asks a person, one invocation at a time, with a deadline shorter than the approval's life."""

    def __init__(
        self, ask: AskCallable | None, *, timeout_s: float = DEFAULT_PROMPT_TIMEOUT_S
    ) -> None:
        if not 0 < timeout_s <= DEFAULT_APPROVAL_TTL_S:
            raise ValueError("timeout_s must be positive and no longer than the approval lifetime")
        self._ask = ask
        self._timeout_s = timeout_s
        self._lock: asyncio.Lock | None = None  # created on first use, inside the running loop

    async def request(self, invocation: InvocationRequest) -> ApprovalDecision | None:
        if self._ask is None:  # nobody to ask: that is a no, not an approval
            return self._decision(invocation, False)
        if self._lock is None:
            self._lock = asyncio.Lock()
        try:
            verdict = await asyncio.wait_for(self._prompt(invocation), self._timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # includes TimeoutError and any UI failure
            return self._decision(invocation, False)
        return self._decision(invocation, verdict is True)

    async def _prompt(self, invocation: InvocationRequest) -> Any:
        assert self._lock is not None and self._ask is not None
        async with self._lock:  # one prompt on screen at a time
            return await self._ask(build_prompt(invocation))

    @staticmethod
    def _decision(invocation: InvocationRequest, approved: bool) -> ApprovalDecision:
        return ApprovalDecision(
            invocation_id=invocation.invocation_id,
            tool=invocation.tool,
            call_digest=invocation.call_digest,
            approved=approved,
        )


def confirm_ask() -> AskCallable:
    """A prompt backed by the REPL's yes/no widget. Defaults to No. Not wired to any command here."""

    async def ask(prompt: ApprovalPrompt) -> Any:
        from velune.cli.interactive import confirm

        return await confirm(f"{prompt.title}\n{prompt.detail}", hint=prompt.hint, default=False)

    return ask


__all__ = [
    "ApprovalPrompt",
    "InteractiveApprovalPort",
    "build_prompt",
    "confirm_ask",
]
