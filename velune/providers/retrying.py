"""Transparent retry wrapper around ModelProvider.infer()/stream().

Wired in once, at :meth:`velune.providers.registry.ProviderRegistry.get`, so
every call site (Council, the native tool loop, the REPL's fallback chat
path) gets automatic retry on transient failures without needing its own
retry logic — previously only the Council orchestrator retried at all
(``velune/cognition/orchestrator.py``); ordinary chat inference had none.

A :class:`~velune.core.errors.provider.RateLimitError` carrying a
``retry_after`` (parsed from the provider's own ``Retry-After`` header — see
``adapters/_http_errors.py``) drives the wait directly instead of blind
exponential backoff, when the provider tells us how long to wait.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

from velune.core.errors.provider import InferenceError, ProviderConnectionError
from velune.core.retry import RetryPolicy, retry_async
from velune.core.types.inference import InferenceRequest, InferenceResponse, StreamChunk
from velune.core.types.model import ModelDescriptor
from velune.core.types.provider import ProviderCapabilities, ProviderHealth
from velune.providers.base import ModelProvider

logger = logging.getLogger("velune.providers.retrying")

# Transient, worth retrying: connection failures and the generic bucket every
# adapter raises for 5xx/429/timeouts. Deliberately excludes
# ProviderAuthenticationError — retrying a rejected key wastes three attempts
# on something retrying can never fix.
RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (InferenceError, ProviderConnectionError)


class RetryingProvider(ModelProvider):
    """Delegates every :class:`ModelProvider` method to *inner*.

    Only ``infer()``/``stream()`` gain retry behavior; everything else
    (``list_models``, ``embed``, ``health_check``, ...) passes straight
    through unchanged.
    """

    def __init__(self, inner: ModelProvider, *, max_attempts: int = 3) -> None:
        self._inner = inner
        # Adapters that support streamed tool-call turns advertise it as a
        # class attribute the tool loop checks via getattr(provider, ...) —
        # copied onto the instance so wrapping a provider never silently
        # downgrades it to the blocking-only path.
        self.SUPPORTS_STREAMING_TOOL_CALLS = getattr(inner, "SUPPORTS_STREAMING_TOOL_CALLS", False)
        self._policy = RetryPolicy(
            max_attempts=max_attempts,
            base_delay_s=1.0,
            max_delay_s=20.0,
            jitter=True,
            retryable_exceptions=RETRYABLE_EXCEPTIONS,
        )

    @property
    def provider_id(self) -> str:
        return self._inner.provider_id

    async def infer(self, request: InferenceRequest) -> InferenceResponse:
        return await retry_async(
            self._policy,
            lambda: self._inner.infer(request),
            source=f"provider.{self._inner.provider_id}",
        )

    async def stream(self, request: InferenceRequest) -> AsyncIterator[StreamChunk]:
        """Retries only a stream that fails before its first chunk arrives.

        Once real content has already reached the caller, restarting the
        request from scratch would duplicate or silently drop what was
        already delivered — safer to let a mid-stream failure propagate
        untouched, exactly as it did before this wrapper existed.
        """
        attempt = 0
        while True:
            attempt += 1
            started = False
            try:
                async for chunk in self._inner.stream(request):
                    started = True
                    yield chunk
                return
            except asyncio.CancelledError:
                raise
            except RETRYABLE_EXCEPTIONS as exc:
                if started or attempt >= self._policy.max_attempts:
                    raise
                delay = getattr(exc, "retry_after", None)
                if delay is None:
                    delay = self._policy._delay(attempt)
                logger.warning(
                    "Retrying %s.stream() attempt %d/%d after %.1fs (%s: %s)",
                    self._inner.provider_id,
                    attempt,
                    self._policy.max_attempts,
                    delay,
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(delay)

    async def list_models(self) -> list[ModelDescriptor]:
        return await self._inner.list_models()

    async def embed(self, texts: list[str], model_id: str) -> list[list[float]]:
        return await self._inner.embed(texts, model_id)

    async def health_check(self) -> ProviderHealth:
        return await self._inner.health_check()

    def get_capabilities(self) -> ProviderCapabilities:
        return self._inner.get_capabilities()

    async def initialize(self) -> None:
        await self._inner.initialize()

    async def authenticate(self) -> None:
        await self._inner.authenticate()

    async def reconnect(self) -> None:
        await self._inner.reconnect()

    async def shutdown(self) -> None:
        await self._inner.shutdown()

    def __getattr__(self, name: str):
        # Fallback for adapter-specific extras (e.g. GroqProvider.get_provider_info)
        # that aren't part of the core ModelProvider protocol.
        return getattr(self._inner, name)
