"""Google Gemini provider adapter — Generative Language REST API."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx

from velune.core.errors.provider import InferenceError, ProviderAuthenticationError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk, ToolCall
from velune.core.types.model import CapabilityLevel, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.base import ModelProvider
from velune.providers.keystore import get_key

_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

_MODELS = [
    ModelDescriptor(
        model_id="gemini-2.0-flash",
        display_name="Gemini 2.0 Flash",
        provider_id="google",
        context_length=1048576,
        capabilities={
            "coding": CapabilityLevel.ADVANCED,
            "reasoning": CapabilityLevel.ADVANCED,
            "planning": CapabilityLevel.ADVANCED,
            "summarization": CapabilityLevel.EXPERT,
            "instruction_following": CapabilityLevel.EXPERT,
            "tool_use": CapabilityLevel.ADVANCED,
            "long_context": CapabilityLevel.EXPERT,
        },
        is_local=False,
        speed_tier="fast",
        cost_per_1k_tokens=0.000075,
        tags=["cloud", "google", "flash", "free"],
    ),
    ModelDescriptor(
        model_id="gemini-1.5-pro",
        display_name="Gemini 1.5 Pro",
        provider_id="google",
        context_length=2097152,
        capabilities={
            "coding": CapabilityLevel.EXPERT,
            "reasoning": CapabilityLevel.EXPERT,
            "planning": CapabilityLevel.EXPERT,
            "summarization": CapabilityLevel.EXPERT,
            "instruction_following": CapabilityLevel.EXPERT,
            "tool_use": CapabilityLevel.EXPERT,
            "long_context": CapabilityLevel.EXPERT,
        },
        is_local=False,
        speed_tier="medium",
        cost_per_1k_tokens=0.00125,
        tags=["cloud", "google", "pro"],
    ),
    ModelDescriptor(
        model_id="gemini-1.5-flash",
        display_name="Gemini 1.5 Flash",
        provider_id="google",
        context_length=1048576,
        capabilities={
            "coding": CapabilityLevel.ADVANCED,
            "reasoning": CapabilityLevel.ADVANCED,
            "planning": CapabilityLevel.INTERMEDIATE,
            "summarization": CapabilityLevel.ADVANCED,
            "instruction_following": CapabilityLevel.EXPERT,
            "tool_use": CapabilityLevel.ADVANCED,
            "long_context": CapabilityLevel.EXPERT,
        },
        is_local=False,
        speed_tier="fast",
        cost_per_1k_tokens=0.000075,
        tags=["cloud", "google", "flash", "free"],
    ),
    ModelDescriptor(
        model_id="gemini-2.0-flash-thinking-exp",
        display_name="Gemini 2.0 Flash Thinking",
        provider_id="google",
        context_length=32767,
        capabilities={
            "coding": CapabilityLevel.EXPERT,
            "reasoning": CapabilityLevel.EXPERT,
            "planning": CapabilityLevel.EXPERT,
            "summarization": CapabilityLevel.ADVANCED,
            "instruction_following": CapabilityLevel.EXPERT,
            "tool_use": CapabilityLevel.ADVANCED,
            "long_context": CapabilityLevel.INTERMEDIATE,
        },
        is_local=False,
        speed_tier="medium",
        cost_per_1k_tokens=0.0,
        tags=["cloud", "google", "thinking", "free"],
    ),
]

# Public alias used by tests and tooling
GEMINI_MODELS = _MODELS


def _build_contents(messages: list[dict]) -> tuple[list[dict], str]:
    """Split messages into Gemini *contents* + system instruction text.

    Assistant ``tool_calls`` become ``functionCall`` parts; ``tool``-role
    messages become ``functionResponse`` parts. Gemini's functionResponse
    needs the function *name*, not an id, so a running id→name map (built
    from each assistant turn's tool_calls) resolves the correlating name for
    the ``tool_call_id`` on each subsequent tool message.
    """
    system_parts: list[str] = []
    contents: list[dict] = []
    id_to_name: dict[str, str] = {}
    for msg in messages:
        role = msg.get("role", "user")
        text = msg.get("content") or ""
        if role == "system":
            system_parts.append(text)
        elif role == "assistant":
            parts: list[dict] = []
            if text:
                parts.append({"text": text})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {}) or {}
                args_raw = fn.get("arguments", "")
                if isinstance(args_raw, dict):
                    args = args_raw
                else:
                    try:
                        args = json.loads(args_raw) if args_raw else {}
                    except json.JSONDecodeError:
                        args = {}
                name = fn.get("name", "")
                id_to_name[tc.get("id", "")] = name
                parts.append({"functionCall": {"name": name, "args": args}})
            contents.append({"role": "model", "parts": parts or [{"text": ""}]})
        elif role == "tool":
            name = id_to_name.get(msg.get("tool_call_id", ""), "")
            contents.append(
                {
                    "role": "user",
                    "parts": [{"functionResponse": {"name": name, "response": {"content": text}}}],
                }
            )
        else:
            contents.append({"role": "user", "parts": [{"text": text}]})
    return contents, "\n".join(system_parts)


def _to_gemini_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Convert OpenAI function-format tool definitions to Gemini's shape."""
    if not tools:
        return None
    declarations = []
    for t in tools:
        fn = t.get("function", t)
        declarations.append(
            {
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return [{"functionDeclarations": declarations}] if declarations else None


def _to_gemini_tool_config(tool_choice: str | dict[str, Any] | None) -> dict[str, Any] | None:
    """Convert an OpenAI-style ``tool_choice`` to Gemini's ``toolConfig``."""
    if tool_choice is None or tool_choice == "auto":
        return None
    if tool_choice == "none":
        return {"functionCallingConfig": {"mode": "NONE"}}
    if tool_choice == "required":
        return {"functionCallingConfig": {"mode": "ANY"}}
    if isinstance(tool_choice, dict):
        name = (tool_choice.get("function") or {}).get("name")
        if name:
            return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}
    return None


def _parse_gemini_tool_calls(parts: list[dict[str, Any]]) -> list[ToolCall] | None:
    """Extract ``functionCall`` parts into normalized ToolCalls (Gemini has no call IDs)."""
    calls = [
        ToolCall(
            id=f"call_{uuid.uuid4().hex[:12]}",
            name=p["functionCall"].get("name", ""),
            arguments=p["functionCall"].get("args") or {},
        )
        for p in parts
        if "functionCall" in p
    ]
    return calls or None


class GoogleProvider(ModelProvider):
    """Google Gemini provider using the Generative Language REST API."""

    # Gemini's SSE stream emits a whole functionCall part per chunk (not
    # fragmented like OpenAI's delta.tool_calls), so accumulation across the
    # stream and a final metadata["tool_calls"] chunk is straightforward.
    SUPPORTS_STREAMING_TOOL_CALLS = True

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or get_key("google")
        self.client: httpx.AsyncClient | None = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=True,
            supports_embeddings=False,
            max_context_window=2097152,
        )

    def _convert_messages(self, request: InferenceRequest) -> dict:
        """Build the full Gemini REST payload from an InferenceRequest."""
        contents, system_text = _build_contents(request.messages)
        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": request.temperature,
                "topP": request.top_p,
                **({"maxOutputTokens": request.max_tokens} if request.max_tokens else {}),
                **({"stopSequences": request.stop_sequences} if request.stop_sequences else {}),
            },
        }
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}
        gemini_tools = _to_gemini_tools(request.tools)
        if gemini_tools:
            payload["tools"] = gemini_tools
            tool_config = _to_gemini_tool_config(request.tool_choice)
            if tool_config:
                payload["toolConfig"] = tool_config
        return payload

    @property
    def provider_id(self) -> str:
        return "google"

    async def initialize(self) -> None:
        if not self._api_key:
            raise ProviderAuthenticationError(
                "Google API key not found — set GOOGLE_API_KEY or run: velune config set-key google"
            )
        if not self.client:
            # Authenticate via header, not a ?key= query param. httpx includes the
            # request URL in its exception text, and those exceptions are wrapped
            # into InferenceError messages that get logged and surfaced — a key in
            # the URL leaks everywhere an error does.
            self.client = httpx.AsyncClient(
                base_url=_BASE_URL,
                timeout=300.0,
                headers={"x-goog-api-key": self._api_key or ""},
            )

    async def list_models(self) -> list[ModelDescriptor]:
        return list(_MODELS)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        payload = self._convert_messages(request)

        try:
            url = f"/models/{request.model_id}:generateContent"
            resp = await self.client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            latency = (time.perf_counter() - start) * 1000.0

            candidate = data.get("candidates", [{}])[0]
            parts = candidate.get("content", {}).get("parts", [])
            text = "".join(p.get("text", "") for p in parts if "text" in p)
            usage = data.get("usageMetadata", {})
            tool_calls = _parse_gemini_tool_calls(parts)
            return InferenceResponse(
                content=text,
                model_id=request.model_id,
                finish_reason=(
                    "tool_calls" if tool_calls else (candidate.get("finishReason") or "STOP").lower()
                ),
                tokens_used=usage.get("totalTokenCount", 0),
                latency_ms=latency,
                tool_calls=tool_calls,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"Google Gemini inference failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        await self.initialize()
        assert self.client is not None
        payload = self._convert_messages(request)

        try:
            url = f"/models/{request.model_id}:streamGenerateContent"
            params = {"alt": "sse"}
            collected_calls: list[ToolCall] = []
            async with self.client.stream("POST", url, json=payload, params=params) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                        candidate = data.get("candidates", [{}])[0]
                        parts = candidate.get("content", {}).get("parts", [])
                        text = "".join(p.get("text", "") for p in parts if "text" in p)
                        new_calls = _parse_gemini_tool_calls(parts)
                        if new_calls:
                            collected_calls.extend(new_calls)
                        finish = candidate.get("finishReason")
                        yield StreamChunk(
                            content=text,
                            finish_reason=finish.lower() if finish else None,
                        )
                    except (json.JSONDecodeError, IndexError):
                        continue

            if collected_calls:
                yield StreamChunk(
                    content="",
                    finish_reason="tool_calls",
                    metadata={"tool_calls": collected_calls},
                )
        except httpx.HTTPError as e:
            raise InferenceError(f"Google Gemini stream failed: {e}")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        raise NotImplementedError("GoogleProvider does not support embeddings via this adapter.")

    async def health_check(self) -> ProviderHealth:
        try:
            await self.initialize()
            assert self.client is not None
            resp = await self.client.get("/models")
            return ProviderHealth.HEALTHY if resp.status_code == 200 else ProviderHealth.DEGRADED
        except Exception:
            return ProviderHealth.UNAVAILABLE

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
