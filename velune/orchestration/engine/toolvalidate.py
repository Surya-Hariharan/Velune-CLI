"""Trusted validation of a proposed tool call. Validation only: no permission, no approval, no run.

``validate_tool_call`` turns an untrusted :class:`ProposedToolCall` into a :class:`ValidatedToolCall`
or raises :class:`ToolCallRejected`. It checks, in order: the tool is in the agent's resolved policy
and the trusted catalog; the arguments are bounded, JSON-compatible data; and they satisfy the tool's
own declared schema, strictly. It then computes digests over the canonical form so a later approval
can be bound to exactly these arguments.

A validated call is *not* an authorization. It names no approver, carries no grant and has no
"allowed" flag; the later permission gate and human approval still decide whether it may run, and
they must do so on the digest, not on anything the model says afterwards.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import Field

from velune._compat import StrEnum
from velune.council.serialization import Contract
from velune.orchestration.engine.errors import EngineError
from velune.orchestration.engine.proposals import ProposedToolCall
from velune.orchestration.engine.toolpolicy import ResolvedToolPolicy, ToolDescriptor
from velune.orchestration.engine.toolschema import (
    ArgumentError,
    canonical_arguments,
    check_arguments,
)

MAX_PROPOSALS_PER_RESPONSE = 16


class ToolRejection(StrEnum):
    NOT_ALLOWED = "not_allowed"
    UNKNOWN_TOOL = "unknown_tool"
    NOT_JSON = "not_json"
    TOO_LARGE = "too_large"
    TOO_DEEP = "too_deep"
    BAD_ARGUMENTS = "bad_arguments"
    DUPLICATE_CALL_ID = "duplicate_call_id"
    TOO_MANY_CALLS = "too_many_calls"


class ToolCallRejected(EngineError):
    """A proposal failed trusted validation. ``kind`` is stable; the message never echoes values."""

    def __init__(self, kind: ToolRejection, call_id: str, message: str) -> None:
        self.kind = kind
        self.call_id = call_id
        self.message = message
        super().__init__(f"tool call {call_id!r} rejected ({kind.value}): {message}")


class ValidatedToolCall(Contract):
    """A proposal that passed validation. Carries no approval and no execution result.

    ``arguments_json`` is the canonical encoding that the digests cover; ``arguments`` returns a
    fresh decoded copy each time so no caller can mutate what was validated.
    """

    call_id: str
    name: str
    arguments_json: str
    # sha256 over the canonical arguments alone, and over the tool name plus arguments. Approval
    # must bind to ``call_digest``: a different tool or different arguments always differ.
    arguments_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    call_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The scopes the tool declares it needs (information for the later permission step).
    declared_permissions: frozenset[str]

    @property
    def arguments(self) -> dict[str, Any]:
        return json.loads(self.arguments_json)


_KIND_OF_ARGUMENT_ERROR: dict[str, ToolRejection] = {
    "not_json": ToolRejection.NOT_JSON,
    "too_large": ToolRejection.TOO_LARGE,
    "too_deep": ToolRejection.TOO_DEEP,
    "schema": ToolRejection.BAD_ARGUMENTS,
}


def argument_digest(canonical_json: str) -> str:
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def call_digest(name: str, canonical_json: str) -> str:
    payload = json.dumps(
        {"tool": name, "arguments": canonical_json}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_tool_call(
    proposal: ProposedToolCall,
    policy: ResolvedToolPolicy,
    catalog: Mapping[str, ToolDescriptor],
) -> ValidatedToolCall:
    """Validate one proposal against the agent's resolved policy and the trusted catalog."""
    name = proposal.name
    if not policy.permits(name):
        raise ToolCallRejected(
            ToolRejection.NOT_ALLOWED,
            proposal.call_id,
            f"tool {name!r} is not available to this agent",
        )
    descriptor = catalog.get(name)
    if descriptor is None:
        raise ToolCallRejected(
            ToolRejection.UNKNOWN_TOOL, proposal.call_id, f"tool {name!r} is not registered"
        )
    try:
        canonical = canonical_arguments(proposal.arguments)
        check_arguments(descriptor.schema_, json.loads(canonical))
    except ArgumentError as exc:
        raise ToolCallRejected(
            _KIND_OF_ARGUMENT_ERROR.get(exc.kind, ToolRejection.BAD_ARGUMENTS),
            proposal.call_id,
            exc.message,
        ) from None
    return ValidatedToolCall(
        call_id=proposal.call_id,
        name=name,
        arguments_json=canonical,
        arguments_digest=argument_digest(canonical),
        call_digest=call_digest(name, canonical),
        declared_permissions=descriptor.permissions,
    )


def validate_proposals(
    proposals: Iterable[ProposedToolCall],
    policy: ResolvedToolPolicy,
    catalog: Mapping[str, ToolDescriptor],
) -> tuple[ValidatedToolCall, ...]:
    """Validate a whole response atomically: one bad proposal rejects the lot (fail closed)."""
    items = tuple(proposals)
    if len(items) > MAX_PROPOSALS_PER_RESPONSE:
        raise ToolCallRejected(
            ToolRejection.TOO_MANY_CALLS,
            items[0].call_id,
            f"more than {MAX_PROPOSALS_PER_RESPONSE} tool calls in one response",
        )
    seen: set[str] = set()
    for item in items:
        if item.call_id in seen:
            raise ToolCallRejected(
                ToolRejection.DUPLICATE_CALL_ID, item.call_id, "call ids must be unique"
            )
        seen.add(item.call_id)
    return tuple(validate_tool_call(item, policy, catalog) for item in items)
