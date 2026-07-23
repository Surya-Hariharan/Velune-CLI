"""Cohere provider adapter."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import SecretStr

from velune.core.errors.provider import InferenceError, ProviderAuthenticationError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk, ToolCall
from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.base import ModelProvider
from velune.providers.keystore import get_key

# OpenAI JSON-Schema property types -> Cohere parameter_definitions types.
_SCHEMA_TYPE_MAP = {
    "string": "str",
    "number": "float",
    "integer": "int",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


def _to_cohere_messages(
    messages: list[dict],
) -> tuple[str, list[dict], str, list[dict] | None]:
    """Convert OpenAI-style messages to Cohere chat history + preamble + tool_results.

    Cohere has no per-message "tool" role: a historical tool-calling turn is
    represented as a CHATBOT history entry carrying ``tool_calls`` followed by
    a TOOL history entry carrying ``tool_results``. Only *trailing* tool
    results (nothing newer follows) are surfaced via the top-level
    ``tool_results`` return value instead of history — that is how Cohere
    expects to be asked to continue a turn after tool execution completes.
    """
    preamble = ""
    history: list[dict] = []
    last_user_msg = ""
    id_to_call: dict[str, dict] = {}
    pending_tool_results: list[dict] = []

    def flush_user() -> None:
        nonlocal last_user_msg
        if last_user_msg:
            history.append({"role": "USER", "message": last_user_msg})
            last_user_msg = ""

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            history.append({"role": "TOOL", "tool_results": pending_tool_results})
            pending_tool_results = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content") or ""
        if role == "system":
            preamble = content
        elif role == "user":
            flush_tool_results()
            flush_user()
            last_user_msg = content
        elif role == "assistant":
            flush_tool_results()
            flush_user()
            raw_calls = msg.get("tool_calls") or []
            if raw_calls:
                calls = []
                for raw in raw_calls:
                    fn = raw.get("function", {}) or {}
                    args_raw = fn.get("arguments", "")
                    if isinstance(args_raw, dict):
                        params = args_raw
                    else:
                        try:
                            params = json.loads(args_raw) if args_raw else {}
                        except json.JSONDecodeError:
                            params = {}
                    call = {"name": fn.get("name", ""), "parameters": params}
                    id_to_call[raw.get("id", "")] = call
                    calls.append(call)
                entry: dict[str, Any] = {"role": "CHATBOT", "tool_calls": calls}
                if content:
                    entry["message"] = content
                history.append(entry)
            else:
                history.append({"role": "CHATBOT", "message": content})
        elif role == "tool":
            call = id_to_call.get(msg.get("tool_call_id", ""), {"name": "", "parameters": {}})
            pending_tool_results.append({"call": call, "outputs": [{"result": content}]})

    # Whatever remains in last_user_msg is the final pending turn — it becomes
    # the returned `message`, not another history entry.
    tool_results = pending_tool_results or None
    return preamble, history, ("" if tool_results else last_user_msg), tool_results


def _to_cohere_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Convert OpenAI function-format tool definitions to Cohere's shape."""
    if not tools:
        return None
    converted: list[dict[str, Any]] = []
    for t in tools:
        fn = t.get("function", t)
        params = fn.get("parameters") or {}
        props = params.get("properties") or {}
        required = set(params.get("required") or [])
        param_defs = {
            name: {
                "description": schema.get("description", ""),
                "type": _SCHEMA_TYPE_MAP.get(schema.get("type", "string"), "str"),
                "required": name in required,
            }
            for name, schema in props.items()
        }
        converted.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameter_definitions": param_defs,
            }
        )
    return converted


def _parse_cohere_tool_calls(raw_calls: list[dict[str, Any]] | None) -> list[ToolCall] | None:
    """Normalize Cohere's ``tool_calls`` (no call IDs — Velune synthesizes them)."""
    if not raw_calls:
        return None
    return [
        ToolCall(
            id=f"call_{uuid.uuid4().hex[:12]}",
            name=raw.get("name", ""),
            arguments=raw.get("parameters") or {},
        )
        for raw in raw_calls
    ] or None


class CohereProvider(ModelProvider):
    """Cohere provider — Command R and embed models."""

    # Cohere's /chat SSE stream emits a whole "tool-calls-generation" event
    # (not fragmented like OpenAI's delta.tool_calls) ahead of "stream-end".
    SUPPORTS_STREAMING_TOOL_CALLS = True

    def __init__(
        self,
        api_key: str | SecretStr | None = None,
        base_url: str = "https://api.cohere.com/v1",
    ) -> None:
        self._api_key = api_key or get_key("cohere")
        if hasattr(self._api_key, "get_secret_value"):
            self._api_key = self._api_key.get_secret_value()
        self._base_url = base_url
        self.client: httpx.AsyncClient | None = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=True,
            supports_embeddings=True,
            max_context_window=128000,
        )

    @property
    def provider_id(self) -> str:
        return "cohere"

    async def initialize(self) -> None:
        if not self._api_key:
            raise ProviderAuthenticationError(
                "Cohere API key not found. Set COHERE_API_KEY or run `velune provider add cohere`."
            )
        if not self.client:
            self.client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "X-Client-Name": "velune",
                },
                timeout=300.0,
            )

    async def list_models(self) -> list[ModelDescriptor]:
        await self.initialize()
        return [
            ModelDescriptor(
                model_id="command-r-plus-08-2024",
                display_name="Command R+ (Aug 2024)",
                provider_id="cohere",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.ADVANCED,
                    "reasoning": CapabilityLevel.EXPERT,
                    "planning": CapabilityLevel.ADVANCED,
                    "summarization": CapabilityLevel.EXPERT,
                    "instruction_following": CapabilityLevel.EXPERT,
                    "tool_use": CapabilityLevel.EXPERT,
                    "long_context": CapabilityLevel.EXPERT,
                },
                is_local=False,
                cost_per_1k_tokens=0.00265,
            ),
            ModelDescriptor(
                model_id="command-r-08-2024",
                display_name="Command R (Aug 2024)",
                provider_id="cohere",
                context_length=128000,
                capabilities={
                    "coding": CapabilityLevel.INTERMEDIATE,
                    "reasoning": CapabilityLevel.ADVANCED,
                    "planning": CapabilityLevel.INTERMEDIATE,
                    "summarization": CapabilityLevel.ADVANCED,
                    "instruction_following": CapabilityLevel.ADVANCED,
                    "tool_use": CapabilityLevel.ADVANCED,
                    "long_context": CapabilityLevel.ADVANCED,
                },
                is_local=False,
                cost_per_1k_tokens=0.000375,
            ),
        ]

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        try:
            preamble, history, message, tool_results = _to_cohere_messages(request.messages)
            payload: dict = {
                "model": request.model_id,
                "message": message,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
            }
            if preamble:
                payload["preamble"] = preamble
            if history:
                payload["chat_history"] = history
            if tool_results:
                payload["tool_results"] = tool_results
            cohere_tools = _to_cohere_tools(request.tools)
            if cohere_tools:
                payload["tools"] = cohere_tools

            response = await self.client.post("/chat", json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0

            meta = data.get("meta", {})
            tokens = meta.get("tokens", {})
            input_tokens = tokens.get("input_tokens", 0)
            output_tokens = tokens.get("output_tokens", 0)
            tool_calls = _parse_cohere_tool_calls(data.get("tool_calls"))

            return InferenceResponse(
                content=data.get("text", ""),
                model_id=request.model_id,
                finish_reason=(
                    "tool_calls" if tool_calls else data.get("finish_reason", "COMPLETE").lower()
                ),
                tokens_used=input_tokens + output_tokens,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                latency_ms=latency,
                tool_calls=tool_calls,
            )
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise ProviderAuthenticationError("Cohere API key is invalid or expired.")
            raise InferenceError(f"Cohere chat failed: {e}")
        except httpx.HTTPError as e:
            raise InferenceError(f"Cohere chat failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        await self.initialize()
        assert self.client is not None
        try:
            preamble, history, message, tool_results = _to_cohere_messages(request.messages)
            payload: dict = {
                "model": request.model_id,
                "message": message,
                "temperature": request.temperature,
                "max_tokens": request.max_tokens,
                "stream": True,
            }
            if preamble:
                payload["preamble"] = preamble
            if history:
                payload["chat_history"] = history
            if tool_results:
                payload["tool_results"] = tool_results
            cohere_tools = _to_cohere_tools(request.tools)
            if cohere_tools:
                payload["tools"] = cohere_tools

            pending_tool_calls: list[ToolCall] | None = None
            async with self.client.stream("POST", "/chat", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        event_type = data.get("event_type", "")
                        if event_type == "text-generation":
                            yield StreamChunk(content=data.get("text", ""))
                        elif event_type == "tool-calls-generation":
                            pending_tool_calls = _parse_cohere_tool_calls(data.get("tool_calls"))
                        elif event_type == "stream-end":
                            resp = data.get("response") or {}
                            if pending_tool_calls is None:
                                pending_tool_calls = _parse_cohere_tool_calls(
                                    resp.get("tool_calls")
                                )
                            yield StreamChunk(
                                content="",
                                finish_reason=(
                                    "tool_calls"
                                    if pending_tool_calls
                                    else data.get("finish_reason", "COMPLETE").lower()
                                ),
                                metadata=(
                                    {"tool_calls": pending_tool_calls}
                                    if pending_tool_calls
                                    else {}
                                ),
                            )
                    except json.JSONDecodeError:
                        continue
        except httpx.HTTPError as e:
            raise InferenceError(f"Cohere stream failed: {e}")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        await self.initialize()
        assert self.client is not None
        try:
            response = await self.client.post(
                "/embed",
                json={
                    "model": model_id or "embed-english-v3.0",
                    "texts": texts,
                    "input_type": "search_document",
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["embeddings"]
        except httpx.HTTPError as e:
            raise InferenceError(f"Cohere embed failed: {e}")

    async def health_check(self) -> ProviderHealth:
        try:
            await self.initialize()
            assert self.client is not None
            resp = await self.client.get("/models")
            if resp.status_code == 200:
                return ProviderHealth.HEALTHY
            if resp.status_code == 401:
                return ProviderHealth.UNAVAILABLE
            return ProviderHealth.DEGRADED
        except Exception:
            return ProviderHealth.UNAVAILABLE

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
