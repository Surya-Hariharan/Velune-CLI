"""Google Gemini provider adapter — Generative Language REST API."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from velune.core.errors.provider import InferenceError, ProviderAuthenticationError
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
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
    """Split messages into Gemini *contents* + system instruction text."""
    system_parts: list[str] = []
    contents: list[dict] = []
    for msg in messages:
        role = msg.get("role", "user")
        text = msg.get("content", "")
        if role == "system":
            system_parts.append(text)
        else:
            gemini_role = "model" if role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": text}]})
    return contents, "\n".join(system_parts)


class GoogleProvider(ModelProvider):
    """Google Gemini provider using the Generative Language REST API."""

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
            self.client = httpx.AsyncClient(base_url=_BASE_URL, timeout=300.0)

    async def list_models(self) -> list[ModelDescriptor]:
        return list(_MODELS)

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        await self.initialize()
        assert self.client is not None
        start = time.perf_counter()
        payload = self._convert_messages(request)

        try:
            url = f"/models/{request.model_id}:generateContent"
            resp = await self.client.post(url, json=payload, params={"key": self._api_key})
            resp.raise_for_status()
            data = resp.json()
            latency = (time.perf_counter() - start) * 1000.0

            candidate = data.get("candidates", [{}])[0]
            text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []))
            usage = data.get("usageMetadata", {})
            return InferenceResponse(
                content=text,
                model_id=request.model_id,
                finish_reason=(candidate.get("finishReason") or "STOP").lower(),
                tokens_used=usage.get("totalTokenCount", 0),
                latency_ms=latency,
            )
        except httpx.HTTPError as e:
            raise InferenceError(f"Google Gemini inference failed: {e}")

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        await self.initialize()
        assert self.client is not None
        payload = self._convert_messages(request)

        try:
            url = f"/models/{request.model_id}:streamGenerateContent"
            params = {"key": self._api_key, "alt": "sse"}
            async with self.client.stream("POST", url, json=payload, params=params) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        data = json.loads(line[6:])
                        candidate = data.get("candidates", [{}])[0]
                        parts = candidate.get("content", {}).get("parts", [])
                        text = "".join(p.get("text", "") for p in parts)
                        finish = candidate.get("finishReason")
                        yield StreamChunk(
                            content=text,
                            finish_reason=finish.lower() if finish else None,
                        )
                    except (json.JSONDecodeError, IndexError):
                        continue
        except httpx.HTTPError as e:
            raise InferenceError(f"Google Gemini stream failed: {e}")

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        raise NotImplementedError("GoogleProvider does not support embeddings via this adapter.")

    async def health_check(self) -> ProviderHealth:
        try:
            await self.initialize()
            assert self.client is not None
            resp = await self.client.get("/models", params={"key": self._api_key})
            return ProviderHealth.HEALTHY if resp.status_code == 200 else ProviderHealth.DEGRADED
        except Exception:
            return ProviderHealth.UNHEALTHY

    def get_capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
