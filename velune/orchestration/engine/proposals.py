"""A proposed tool call: untrusted model output, and nothing else.

A :class:`ProposedToolCall` is what an acting agent *asks for*. It names a tool and arguments and
carries a call id and an optional rationale. It cannot carry a permission, an approval, a role, an
artifact label, a result or any other trusted data: the type is frozen and rejects unknown fields,
so ``{"approved": true}`` or ``{"agent": "supervisor"}`` is a validation error, not a hint.

Nothing in this module checks, authorizes or runs a proposal. Trusted validation lives in
``toolvalidate`` and policy in ``toolpolicy``; execution does not exist yet.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field, StringConstraints

from velune.council.serialization import Contract

ToolNameStr = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")]
CallId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")]

MAX_RATIONALE_CHARS = 300


class ProposedToolCall(Contract):
    """One tool call the model wants made. Untrusted: the arguments are validated before any use."""

    call_id: CallId
    name: ToolNameStr
    # Left unvalidated here on purpose: shape, size and depth are judged by trusted validation, which
    # must see exactly what the model sent. Treat it as hostile until validated.
    arguments: dict[str, Any] = Field(default_factory=dict)
    rationale: Annotated[str, StringConstraints(max_length=MAX_RATIONALE_CHARS)] = ""
