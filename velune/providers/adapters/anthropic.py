"""Anthropic provider adapter implementation."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx
from pydantic import SecretStr

from velune.core.errors.provider import ProviderAuthenticationError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk, ToolCall
from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.adapters._http_errors import raise_typed_http_error
from velune.providers.adapters._live_catalog import get_json, reconcile
from velune.providers.base import ModelProvider
from velune.providers.keystore import get_key

_ANTHROPIC_VERSION = "2023-06-01"


def anthropic_profile(model_id: str) -> ModelCapabilityProfile:
    """Capability profile by model tier (the API reports none)."""
    mid = model_id.lower()
    expert, adv, mid_lvl = (
        CapabilityLevel.EXPERT,
        CapabilityLevel.ADVANCED,
        CapabilityLevel.INTERMEDIATE,
    )
    if "opus" in mid or "fable" in mid:
        return ModelCapabilityProfile(
            coding=expert,
            reasoning=expert,
            planning=expert,
            summarization=expert,
            instruction_following=expert,
            tool_use=expert,
            long_context=expert,
            vision=expert,
            multimodal=expert,
        )
    if "haiku" in mid:
        return ModelCapabilityProfile(
            coding=mid_lvl,
            reasoning=mid_lvl,
            planning=mid_lvl,
            summarization=mid_lvl,
            instruction_following=adv,
            tool_use=adv,
            long_context=mid_lvl,
            vision=mid_lvl,
            multimodal=mid_lvl,
        )
    return ModelCapabilityProfile(  # sonnet and anything newer/unknown
        coding=adv,
        reasoning=adv,
        planning=adv,
        summarization=adv,
        instruction_following=adv,
        tool_use=expert,
        long_context=adv,
        vision=adv,
        multimodal=adv,
    )


def _anthropic_curated(model_id: str, name: str, cost: float, speed: str) -> ModelDescriptor:
    return ModelDescriptor(
        model_id=model_id,
        display_name=name,
        provider_id="anthropic",
        context_length=200000,
        capabilities=anthropic_profile(model_id),
        speed_tier=speed,
        cost_per_1k_tokens=cost,
        location="cloud",
        tags=["cloud", "anthropic"],
        is_local=False,
    )


ANTHROPIC_MODELS: list[ModelDescriptor] = [
    _anthropic_curated("claude-opus-4-5", "Claude Opus 4.5", 0.015, "medium"),
    _anthropic_curated("claude-sonnet-4-5", "Claude Sonnet 4.5", 0.003, "medium"),
    _anthropic_curated("claude-haiku-4-5", "Claude Haiku 4.5", 0.00025, "fast"),
]


async def fetch_live_models(api_key: str | None, base_url: str) -> dict[str, int | None] | None:
    """Return ``{model_id: max_input_tokens}`` from ``GET /v1/models``, or None on failure."""
    if not api_key:
        return None
    data = await get_json(
        f"{base_url}/v1/models",
        {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION},
        params={"limit": 1000},
    )
    if not isinstance(data, dict):
        return None
    return {
        m["id"]: m.get("max_input_tokens")
        for m in data.get("data", [])
        if isinstance(m, dict) and "id" in m
    }


async def discover_anthropic_models(
    api_key: str | None, base_url: str = "https://api.anthropic.com"
) -> list[ModelDescriptor]:
    """Curated Claude catalog reconciled with the live listing (offline: curated as-is)."""
    live = await fetch_live_models(api_key, base_url)
    models = reconcile(
        ANTHROPIC_MODELS,
        live,
        "anthropic",
        accept=lambda mid: mid.lower().startswith("claude"),
        fallback_window=lambda _mid: 200000,
    )
    for m in models:
        if "live-discovered" in m.tags:
            m.capabilities = anthropic_profile(m.model_id)
    return models


class AnthropicProvider(ModelProvider):
    """Anthropic provider for Claude models."""

    # stream() accumulates tool_use blocks (input_json_delta) and emits a
    # final tool-call chunk — the tool loop may stream turns with tools.
    SUPPORTS_STREAMING_TOOL_CALLS = True

    def __init__(
        self, api_key: str | SecretStr | None = None, base_url: str = "https://api.anthropic.com"
    ) -> None:
        self._api_key = api_key or get_key("anthropic")
        if hasattr(self._api_key, "get_secret_value"):
            self._api_key = self._api_key.get_secret_value()
        self._base_url = base_url
        self.client: httpx.AsyncClient | None = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            supports_function_calling=True,
            supports_embeddings=False,
            max_context_window=200000,
        )

    @property
    def provider_id(self) -> str:
        return "anthropic"

    def _raise_provider_error(self, exc: httpx.HTTPError, action: str) -> None:
        """Map an httpx failure to the right typed error.

        A 401/403 is a verdict about the key; a 429 is a rate limit whose
        ``Retry-After`` header (when present) should drive the retry delay
        instead of blind exponential backoff — see
        :class:`velune.providers.retrying.RetryingProvider`. Everything else
        is a generic :class:`InferenceError`.
        """
        raise_typed_http_error("Anthropic", exc, action)

    async def initialize(self) -> None:
        """Initialize HTTP client with Anthropic specific headers."""
        if not self._api_key:
            raise ProviderAuthenticationError(
                "Anthropic API key not found in configuration or environment"
            )
        if not self.client:
            headers = {
                "x-api-key": self._api_key,
                "anthropic-version": _ANTHROPIC_VERSION,
                # Enable prompt-caching beta. Harmless when no cache_control blocks
                # are present in the payload — activates automatically when they are.
                "anthropic-beta": "prompt-caching-2024-07-31",
                "content-type": "application/json",
            }
            self.client = httpx.AsyncClient(base_url=self._base_url, headers=headers, timeout=300.0)

    async def list_models(self) -> list[ModelDescriptor]:
        """List Claude models, reconciled with what the account can actually call."""
        await self.initialize()
        return await discover_anthropic_models(self._api_key, self._base_url)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Perform Claude inference."""
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        try:
            payload = self._build_payload(request)

            response = await self.client.post("/v1/messages", json=payload)
            response.raise_for_status()
            data = response.json()
            latency = (time.perf_counter() - start) * 1000.0

            # Concatenate all text blocks and normalize tool_use blocks.
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []
            for block in data.get("content") or []:
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append(
                        ToolCall(
                            id=block.get("id", ""),
                            name=block.get("name", ""),
                            arguments=block.get("input") or {},
                        )
                    )
            content = "".join(text_parts)

            # Record latency in health monitor if available
            self._record_latency_to_monitor(latency)

            usage = data.get("usage", {})
            return InferenceResponse(
                content=content,
                model_id=request.model_id,
                finish_reason=(
                    "tool_calls" if tool_calls else (data.get("stop_reason") or "end_turn")
                ),
                tokens_used=usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                latency_ms=latency,
                cache_creation_tokens=usage.get("cache_creation_input_tokens", 0),
                cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                metadata={"raw_usage": usage},
                tool_calls=tool_calls or None,
            )
        except httpx.HTTPError as e:
            self._raise_provider_error(e, "completion")
            raise  # unreachable — _raise_provider_error always raises

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Perform streaming completion."""
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        first_token = True
        from velune.providers.adapters._toolcalls import AnthropicStreamToolAccumulator

        accumulator = AnthropicStreamToolAccumulator()
        try:
            payload = self._build_payload(request)
            payload["stream"] = True

            async with self.client.stream("POST", "/v1/messages", json=payload) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        try:
                            data = json.loads(data_str)
                            d_type = data.get("type")
                            if d_type == "content_block_start":
                                accumulator.on_block_start(
                                    data.get("index", 0), data.get("content_block", {})
                                )
                            elif d_type == "content_block_delta":
                                delta = data.get("delta", {})
                                if delta.get("type") == "input_json_delta":
                                    accumulator.on_input_json_delta(
                                        data.get("index", 0), delta.get("partial_json", "")
                                    )
                                    continue
                                # Record latency at first token
                                if first_token:
                                    latency = (time.perf_counter() - start) * 1000.0
                                    self._record_latency_to_monitor(latency)
                                    first_token = False
                                yield StreamChunk(
                                    content=delta.get("text", ""),
                                )
                            elif d_type == "message_delta":
                                yield StreamChunk(
                                    content="",
                                    finish_reason=data.get("delta", {}).get("stop_reason"),
                                )
                        except (json.JSONDecodeError, KeyError):
                            continue

            tool_calls = accumulator.finalize()
            if tool_calls:
                yield StreamChunk(
                    content="",
                    finish_reason="tool_calls",
                    metadata={"tool_calls": tool_calls},
                )
        except httpx.HTTPError as e:
            self._raise_provider_error(e, "stream")
            raise  # unreachable — _raise_provider_error always raises

    def _build_payload(self, request: InferenceRequest) -> dict:
        """Build the Anthropic API payload for *request*.

        When the cache manager has pre-transformed the messages into
        cache_control block format (stored in metadata), use that directly.
        Otherwise fall back to plain string extraction.
        """
        from velune.context.cache.providers import ANTHROPIC_CACHE_PAYLOAD_KEY

        cache_payload = request.metadata.get(ANTHROPIC_CACHE_PAYLOAD_KEY)
        if cache_payload:
            payload: dict = {
                "model": request.model_id,
                "max_tokens": request.max_tokens or 4096,
                "temperature": request.temperature,
                "top_p": request.top_p,
                "messages": cache_payload["messages"],
            }
            if "system" in cache_payload:
                payload["system"] = cache_payload["system"]
        else:
            system_prompt = ""
            anth_messages = []
            for msg in request.messages:
                if msg.get("role") == "system":
                    system_prompt = msg.get("content", "")
                else:
                    anth_messages.append(self._translate_message(msg))
            payload = {
                "model": request.model_id,
                "messages": anth_messages,
                "max_tokens": request.max_tokens or 4096,
                "temperature": request.temperature,
                "top_p": request.top_p,
            }
            if system_prompt:
                payload["system"] = system_prompt

        if request.stop_sequences:
            payload["stop_sequences"] = request.stop_sequences
        if request.tools:
            payload["tools"] = [self._translate_tool(t) for t in request.tools]
            if request.tool_choice == "required":
                payload["tool_choice"] = {"type": "any"}
            elif request.tool_choice == "none":
                payload["tool_choice"] = {"type": "none"}
            elif isinstance(request.tool_choice, dict):
                name = request.tool_choice.get("function", {}).get("name")
                if name:
                    payload["tool_choice"] = {"type": "tool", "name": name}
        return payload

    @staticmethod
    def _translate_tool(tool: dict) -> dict:
        """OpenAI function-format tool definition → Anthropic tool definition."""
        fn = tool.get("function", tool)
        return {
            "name": fn.get("name", ""),
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        }

    @staticmethod
    def _translate_message(msg: dict) -> dict:
        """One OpenAI-normal-form message → Anthropic Messages API format.

        Velune's internal normal form is the OpenAI chat shape (see
        ``InferenceRequest``): assistant ``tool_calls`` become ``tool_use``
        content blocks and role ``tool`` results become user ``tool_result``
        blocks. Plain string messages pass through unchanged.
        """
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            blocks: list[dict] = []
            if msg.get("content"):
                blocks.append({"type": "text", "text": msg["content"]})
            for call in msg["tool_calls"]:
                fn = call.get("function", {}) or {}
                args_raw = fn.get("arguments", {})
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw) if args_raw else {}
                    except json.JSONDecodeError:
                        args = {"_raw_arguments": args_raw}
                else:
                    args = args_raw or {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": args,
                    }
                )
            return {"role": "assistant", "content": blocks}
        if role == "tool":
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": str(msg.get("content", "")),
                        **({"is_error": True} if msg.get("is_error") else {}),
                    }
                ],
            }
        return {"role": role, "content": msg.get("content")}

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        raise NotImplementedError("Anthropic provider does not support embeddings.")

    def _record_latency_to_monitor(self, latency_ms: float) -> None:
        """Record latency to health monitor if available."""
        try:
            from velune.kernel.registry import get_container

            container = get_container()
            if container.has("runtime.provider_health_monitor"):
                monitor = container.get("runtime.provider_health_monitor")
                monitor.record_latency(self.provider_id, int(latency_ms))
        except (ImportError, AttributeError, KeyError):
            pass  # Health monitor not available, skip

    async def health_check(self) -> ProviderHealth:
        """Simple validation verification."""
        try:
            await self.initialize()
            assert self.client is not None
            # Fetch a simple request with 1 token output
            payload = {
                "model": "claude-haiku-4-5",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1,
            }
            resp = await self.client.post("/v1/messages", json=payload)
            if resp.status_code == 200:
                return ProviderHealth.HEALTHY
            return ProviderHealth.DEGRADED
        except Exception:
            return ProviderHealth.UNAVAILABLE

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
