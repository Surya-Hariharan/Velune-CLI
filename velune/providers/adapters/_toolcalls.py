"""Shared tool-calling wire helpers for provider adapters.

Velune's internal normal form for tools and tool calls is the OpenAI chat
format (see :class:`velune.core.types.inference.InferenceRequest`). These
helpers keep every OpenAI-compatible adapter (OpenAI, Groq, OpenRouter,
openai-compat, LM Studio, vLLM, …) and the Ollama adapter on one tolerant
parsing path instead of four copies.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from velune.core.types.inference import InferenceRequest, ToolCall


def attach_openai_tools(payload: dict[str, Any], request: InferenceRequest) -> None:
    """Add ``tools``/``tool_choice`` to an OpenAI-format *payload* in place.

    No-op when the request carries no tools, so non-tool callers produce
    byte-identical payloads to the pre-tool-calling behavior.
    """
    if request.tools:
        payload["tools"] = request.tools
        if request.tool_choice is not None:
            payload["tool_choice"] = request.tool_choice


def parse_openai_tool_calls(message: dict[str, Any]) -> list[ToolCall] | None:
    """Normalize an OpenAI response ``message.tool_calls`` array.

    Returns None when the message contains no tool calls. Malformed entries
    (missing name, unparseable argument JSON) are kept with best-effort
    fallbacks rather than dropped, so the tool loop can surface the error to
    the model instead of silently losing a call.
    """
    raw_calls = message.get("tool_calls")
    if not raw_calls:
        return None

    calls: list[ToolCall] = []
    for raw in raw_calls:
        fn = raw.get("function", {}) or {}
        args_raw = fn.get("arguments", "")
        if isinstance(args_raw, dict):
            args = args_raw
        else:
            try:
                args = json.loads(args_raw) if args_raw else {}
            except (json.JSONDecodeError, TypeError):
                args = {"_raw_arguments": str(args_raw)}
        if not isinstance(args, dict):
            args = {"_raw_arguments": args}
        calls.append(
            ToolCall(
                id=raw.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                name=fn.get("name", ""),
                arguments=args,
            )
        )
    return calls or None


def parse_ollama_tool_calls(message: dict[str, Any]) -> list[ToolCall] | None:
    """Normalize Ollama ``message.tool_calls`` (arguments are already dicts).

    Ollama does not assign call IDs; synthesize stable unique ones so the
    loop's ``tool_call_id`` correlation works identically across providers.
    """
    raw_calls = message.get("tool_calls")
    if not raw_calls:
        return None

    calls: list[ToolCall] = []
    for raw in raw_calls:
        fn = raw.get("function", {}) or {}
        args = fn.get("arguments") or {}
        if not isinstance(args, dict):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {"_raw_arguments": str(args)}
        calls.append(
            ToolCall(
                id=raw.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                name=fn.get("name", ""),
                arguments=args if isinstance(args, dict) else {"_raw_arguments": args},
            )
        )
    return calls or None
