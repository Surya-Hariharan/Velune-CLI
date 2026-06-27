"""ContextCacheProvider Protocol and concrete implementations.

New providers plug in here without touching orchestration or agent code.

Supported today:
    AnthropicPromptCacheProvider  — prompt-caching-2024-07-31 beta
    NoOpCacheProvider             — passthrough for all other providers

Future (interface already compatible):
    OpenAIAutoPrefixCacheProvider
    OllamaSessionCacheProvider
    VllmPrefixCacheProvider
"""

from __future__ import annotations

import copy
import logging
import os
from typing import TYPE_CHECKING, Any

from velune.context.cache.metrics import CacheMetrics

if TYPE_CHECKING:
    from velune.core.types.inference import InferenceRequest

logger = logging.getLogger("velune.context.cache.providers")
_DEBUG = os.environ.get("VELUNE_DEBUG_CACHE", "").lower() in ("1", "true", "yes")

# Key used to store the pre-transformed Anthropic payload in InferenceRequest.metadata.
# The Anthropic adapter checks for this key and uses it instead of the plain extraction.
ANTHROPIC_CACHE_PAYLOAD_KEY = "_anthropic_cache_payload"


class ContextCacheProvider:
    """Base class / duck-type interface for context cache providers.

    Concrete implementations only need to override the three methods below.
    Using a class (not Protocol) so subclasses can share helpers without
    requiring runtime_checkable overhead.
    """

    @property
    def provider_id(self) -> str:
        raise NotImplementedError

    def supports_caching(self) -> bool:
        return False

    def prepare_request(
        self,
        request: InferenceRequest,
        cacheable_indices: list[int],
    ) -> InferenceRequest:
        """Return a (possibly annotated) copy of *request* with cache hints applied.

        *cacheable_indices* is a list of integers:
            -1  → the system message
             0+ → user/assistant messages[index] in the non-system message list
        """
        return request

    def extract_cache_stats(self, response_metadata: dict[str, Any]) -> CacheMetrics:
        """Parse provider-specific cache token counts from *response_metadata*."""
        return CacheMetrics()


# ---------------------------------------------------------------------------
# Anthropic — prompt-caching-2024-07-31
# ---------------------------------------------------------------------------


class AnthropicPromptCacheProvider(ContextCacheProvider):
    """Injects Anthropic cache_control blocks and parses cache token counts.

    The Anthropic API requires:
    - ``anthropic-beta: prompt-caching-2024-07-31`` header (set in adapter)
    - system: list[block] instead of a plain string
    - message content: list[block] instead of a plain string
    where the *last* block in the cacheable prefix carries ``cache_control``.

    This provider transforms the InferenceRequest into that format and stores
    the result in ``request.metadata[ANTHROPIC_CACHE_PAYLOAD_KEY]`` so the
    Anthropic adapter can use it without any other changes to call-site code.
    """

    @property
    def provider_id(self) -> str:
        return "anthropic"

    def supports_caching(self) -> bool:
        return True

    def prepare_request(
        self,
        request: InferenceRequest,
        cacheable_indices: list[int],
    ) -> InferenceRequest:
        if not cacheable_indices:
            return request

        # Deep-copy metadata so we never mutate the original request.
        new_metadata = copy.deepcopy(request.metadata)

        # Separate system from user/assistant messages.
        system_content: str = ""
        non_system: list[dict[str, Any]] = []
        for msg in request.messages:
            if msg.get("role") == "system":
                system_content = msg.get("content", "")
            else:
                non_system.append(dict(msg))

        cache_payload: dict[str, Any] = {}

        # --- System message ---
        if -1 in cacheable_indices and system_content:
            cache_payload["system"] = [
                {
                    "type": "text",
                    "text": system_content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            if _DEBUG:
                logger.debug(
                    "[cache/anthropic] system block marked ephemeral (%d chars)",
                    len(system_content),
                )
        elif system_content:
            cache_payload["system"] = system_content

        # --- User/assistant messages ---
        transformed_messages: list[dict[str, Any]] = []
        for idx, msg in enumerate(non_system):
            if idx in cacheable_indices:
                content_str = msg.get("content", "")
                transformed_messages.append(
                    {
                        "role": msg["role"],
                        "content": [
                            {
                                "type": "text",
                                "text": content_str,
                                "cache_control": {"type": "ephemeral"},
                            }
                        ],
                    }
                )
                if _DEBUG:
                    logger.debug(
                        "[cache/anthropic] message[%d] role=%r marked ephemeral (%d chars)",
                        idx,
                        msg["role"],
                        len(content_str),
                    )
            else:
                transformed_messages.append(
                    {
                        "role": msg["role"],
                        "content": msg.get("content", ""),
                    }
                )

        cache_payload["messages"] = transformed_messages
        new_metadata[ANTHROPIC_CACHE_PAYLOAD_KEY] = cache_payload

        return request.model_copy(update={"metadata": new_metadata})

    def extract_cache_stats(self, response_metadata: dict[str, Any]) -> CacheMetrics:
        """Parse cache_creation_input_tokens / cache_read_input_tokens from usage block."""
        usage = response_metadata.get("raw_usage", {})
        creation = int(usage.get("cache_creation_input_tokens", 0))
        reads = int(usage.get("cache_read_input_tokens", 0))
        metrics = CacheMetrics()
        if creation:
            metrics.writes += 1
            metrics.cached_input_tokens += creation
        if reads:
            metrics.hits += 1
            metrics.cache_read_tokens += reads
        return metrics


# ---------------------------------------------------------------------------
# No-op — passthrough for all other providers
# ---------------------------------------------------------------------------


class NoOpCacheProvider(ContextCacheProvider):
    """Passthrough provider — returns request unchanged.

    Used for OpenAI, Ollama, Gemini, Groq, etc. until those providers have
    their own cache implementations.
    """

    @property
    def provider_id(self) -> str:
        return "noop"

    def supports_caching(self) -> bool:
        return False

    def prepare_request(
        self, request: InferenceRequest, cacheable_indices: list[int]
    ) -> InferenceRequest:
        return request

    def extract_cache_stats(self, response_metadata: dict[str, Any]) -> CacheMetrics:
        return CacheMetrics()
