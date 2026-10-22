"""NVIDIA NIM provider adapter — OpenAI-compatible API at integrate.api.nvidia.com."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator

import httpx
from pydantic import SecretStr

from velune.core.errors.provider import InferenceError, ProviderAuthenticationError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import CapabilityLevel, ModelCapabilityProfile, ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.adapters._http_errors import raise_typed_http_error
from velune.providers.adapters._messages import openai_messages
from velune.providers.base import ModelProvider
from velune.providers.keystore import get_key

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"

# NVIDIA's catalog mixes chat models with embedding, reranking, retrieval,
# vision-encoder, image-generation and speech models, and its /models listing
# has no "type" field. These id fragments mark the ones that cannot serve
# /chat/completions; listing them as chat models gave users models that 400
# or 404 on the first message.
_NON_CHAT_MARKERS = (
    "embed",
    "rerank",
    "retriever",
    "nv-clip",
    "-parse",
    "sdxl",
    "stable-diffusion",
    "flux",
    "-tts",
    "fastpitch",
    "asr",
    "whisper",
    "canary",
    "parakeet",
)

# Small, long-lived hosted models used to confirm a key with a 1-token
# completion (see validation._validate_nvidia). Tried in order, and only if the
# account's own listing contains them.
_PROBE_MODELS = (
    "meta/llama-3.1-8b-instruct",
    "meta/llama-3.2-3b-instruct",
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-70b-instruct",
)


def is_nim_chat_model(model_id: str) -> bool:
    """False for NIM catalog entries that cannot serve chat completions."""
    mid = model_id.lower()
    return not any(marker in mid for marker in _NON_CHAT_MARKERS)


def pick_probe_model(model_ids: list[str]) -> str | None:
    """A listed chat model suitable for a 1-token key check, or None."""
    listed = set(model_ids)
    for candidate in _PROBE_MODELS:
        if candidate in listed:
            return candidate
    for mid in model_ids:
        if is_nim_chat_model(mid) and "instruct" in mid.lower():
            return mid
    return None


def nim_capabilities(model_id: str) -> ModelCapabilityProfile:
    """Capability profile for a hosted NIM chat model, from its vendor id.

    NVIDIA's listing carries no capability metadata; the curated vendor ids
    (``meta/llama-3.3-70b-instruct``, ``deepseek-ai/deepseek-r1``) are the only
    signal. Matching is on whole id tokens, so ``r1`` or ``vl`` inside an
    unrelated word no longer flips a capability.
    """
    lower = model_id.lower()
    tokens = set(re.split(r"[/\-_.:]", lower))
    profile = ModelCapabilityProfile(
        coding=CapabilityLevel.INTERMEDIATE,
        summarization=CapabilityLevel.INTERMEDIATE,
    )
    if tokens & {"vision", "vl", "vlm", "llava", "neva", "vila"}:
        profile.vision = CapabilityLevel.ADVANCED
        profile.multimodal = CapabilityLevel.ADVANCED
    if tokens & {"r1", "reasoning", "qwq", "think"} or "reason" in lower:
        profile.reasoning = CapabilityLevel.EXPERT
        profile.planning = CapabilityLevel.ADVANCED
    elif "llama" in lower or "mistral" in lower or "nemotron" in lower:
        profile.reasoning = CapabilityLevel.ADVANCED
    if "coder" in lower or "codestral" in lower or "starcoder" in lower:
        profile.coding = CapabilityLevel.ADVANCED
    if tokens & {"instruct", "chat", "it"} or "nemotron" in lower:
        profile.instruction_following = CapabilityLevel.ADVANCED
    return profile


class NVIDIAProvider(ModelProvider):
    """NVIDIA NIM provider — cloud-hosted inference via integrate.api.nvidia.com."""

    def __init__(
        self,
        api_key: str | SecretStr | None = None,
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._api_key = api_key or get_key("nvidia")
        if hasattr(self._api_key, "get_secret_value"):
            self._api_key = self._api_key.get_secret_value()
        self._base_url = base_url.rstrip("/")
        self.client: httpx.AsyncClient | None = None
        self._capabilities = ProviderCapabilities(
            supports_streaming=True,
            # Tool support differs per hosted model and is not advertised by the
            # API; attaching tools to a model without it returns HTTP 400.
            supports_function_calling=False,
            supports_embeddings=False,
            max_context_window=128000,
        )

    @property
    def provider_id(self) -> str:
        return "nvidia"

    async def initialize(self) -> None:
        if not self._api_key:
            raise ProviderAuthenticationError(
                "NVIDIA API key not found. Run /connect nvidia (or set NVIDIA_API_KEY)."
            )
        if not self.client:
            self.client = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=300.0,
            )

    async def authenticate(self) -> None:
        """Confirm the key with a 1-token completion.

        ``GET /models`` is answered without checking the key, so it cannot
        authenticate anything — see ``validation._validate_nvidia``.
        """
        from velune.providers.validation import ValidationStatus, validate_provider

        result = await validate_provider("nvidia", str(self._api_key or ""))
        if result.rejects_key:
            raise ProviderAuthenticationError(result.message)
        if not result.ok and result.status != ValidationStatus.UNCONFIRMED:
            raise InferenceError(result.message)

    async def reconnect(self) -> None:
        """Attempt to re-establish connection."""
        await self.shutdown()
        await self.initialize()
        await self.authenticate()

    async def list_models(self) -> list[ModelDescriptor]:
        await self.initialize()
        assert self.client is not None
        try:
            resp = await self.client.get("/models")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            raise_typed_http_error("NVIDIA NIM", e, "model listing")

        models = []
        for item in data.get("data", []):
            model_id = item.get("id") if isinstance(item, dict) else None
            if not model_id or not is_nim_chat_model(model_id):
                continue
            models.append(
                ModelDescriptor(
                    model_id=model_id,
                    display_name=model_id.split("/")[-1],
                    provider_id="nvidia",
                    context_length=128000,
                    capabilities=nim_capabilities(model_id),
                    is_local=False,
                    location="cloud",
                    tags=["cloud", "nvidia"],
                )
            )
        return models

    def _payload(self, request: InferenceRequest, *, stream: bool) -> dict:
        payload: dict = {
            "model": request.model_id,
            "messages": openai_messages(request.messages),
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "top_p": request.top_p,
        }
        if stream:
            payload["stream"] = True
        if request.stop_sequences:
            payload["stop"] = request.stop_sequences
        return payload

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        try:
            response = await self.client.post(
                "/chat/completions", json=self._payload(request, stream=False)
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            raise_typed_http_error("NVIDIA NIM", e, "completion")
        except ValueError as e:
            raise InferenceError(f"NVIDIA NIM returned an unreadable response: {e}") from e

        choices = data.get("choices") or []
        if not choices:
            raise InferenceError("NVIDIA NIM returned no choices for this request.")
        choice = choices[0]
        message = choice.get("message") or {}
        latency = (time.perf_counter() - start) * 1000.0
        usage = data.get("usage") or {}
        return InferenceResponse(
            content=message.get("content") or "",
            model_id=request.model_id,
            finish_reason=choice.get("finish_reason") or "stop",
            tokens_used=usage.get("total_tokens", 0),
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency,
        )

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        await self.initialize()
        assert self.client is not None
        try:
            async with self.client.stream(
                "POST", "/chat/completions", json=self._payload(request, stream=True)
            ) as response:
                if response.is_error:
                    # Read the body first so the typed error can quote the
                    # provider's own message (wrong model id, bad parameter).
                    await response.aread()
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    choices = data.get("choices") if isinstance(data, dict) else None
                    if not choices:
                        # Usage-only or keep-alive chunk.
                        continue
                    choice = choices[0] or {}
                    delta = choice.get("delta") or {}
                    content = delta.get("content") or ""
                    finish = choice.get("finish_reason")
                    if content or finish:
                        yield StreamChunk(content=content, finish_reason=finish)
        except httpx.HTTPError as e:
            raise_typed_http_error("NVIDIA NIM", e, "streaming completion")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        raise NotImplementedError("Use NVIDIA NIM embedding-specific endpoints directly.")

    async def health_check(self) -> ProviderHealth:
        """Reachability of the API. Not proof the key works (see authenticate)."""
        try:
            await self.initialize()
            assert self.client is not None
            resp = await self.client.get("/models")
            if resp.status_code == 200:
                return ProviderHealth.HEALTHY
            if resp.status_code in (401, 403):
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
