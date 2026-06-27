"""ContextCacheManager — the single facade used by all call sites.

Usage in BaseCouncilAgent.deliberate():

    request = self._cache_manager.prepare(request)
    response = await self.provider.infer(request)
    self._cache_manager.record(response.metadata)

Everything else — fingerprinting, cache-control injection, metrics — is
handled internally. Call sites never import fingerprint.py or providers.py.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from velune.context.cache.fingerprint import ContextFingerprinter
from velune.context.cache.metrics import CacheMetrics
from velune.context.cache.providers import (
    AnthropicPromptCacheProvider,
    ContextCacheProvider,
    NoOpCacheProvider,
)
from velune.context.cache.state import CacheState

logger = logging.getLogger("velune.context.cache.manager")
_DEBUG = os.environ.get("VELUNE_DEBUG_CACHE", "").lower() in ("1", "true", "yes")


class ContextCacheManager:
    """Orchestrates fingerprinting, cache-hint injection, and metrics recording.

    One instance per agent (stored as ``self._cache_manager``) so each agent
    maintains its own stable fingerprint history across consecutive calls.
    """

    def __init__(
        self,
        cache_provider: ContextCacheProvider,
        state: CacheState | None = None,
    ) -> None:
        self._provider = cache_provider
        self._state = state or CacheState()
        self._metrics = CacheMetrics()
        self._fp = ContextFingerprinter()

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def prepare(self, request: Any) -> Any:
        """Fingerprint stable segments, inject cache hints, return annotated request.

        If the provider does not support caching (NoOp), returns *request*
        unchanged so there is zero overhead for non-Anthropic providers.
        """
        if not self._provider.supports_caching():
            if _DEBUG:
                logger.debug(
                    "[cache] provider=%r does not support caching — passthrough",
                    self._provider.provider_id,
                )
            return request

        cacheable_indices: list[int] = []

        # --- System message ---
        system_content = self._extract_system(request)
        if system_content:
            sys_fp = self._fp.fingerprint(system_content)
            if self._state.is_valid("system", sys_fp):
                action = "hit"
                self._metrics.hits += 1
            else:
                action = "write"
                self._metrics.writes += 1
            self._state.update("system", sys_fp, action=action)
            cacheable_indices.append(-1)

            if _DEBUG:
                logger.debug(
                    "[cache] system segment fp=%s action=%s",
                    sys_fp,
                    action.upper(),
                )

        # --- First user message (repo context) ---
        first_user_content = self._extract_first_user(request)
        if first_user_content:
            user_fp = self._fp.fingerprint(first_user_content)
            if self._state.is_valid("first_user", user_fp):
                action = "hit"
                self._metrics.hits += 1
            else:
                action = "write"
                self._metrics.writes += 1
            self._state.update("first_user", user_fp, action=action)
            cacheable_indices.append(0)

            if _DEBUG:
                logger.debug(
                    "[cache] first_user segment fp=%s action=%s",
                    user_fp,
                    action.upper(),
                )

        if _DEBUG:
            logger.debug(
                "[cache] provider=%r cacheable_indices=%r",
                self._provider.provider_id,
                cacheable_indices,
            )

        return self._provider.prepare_request(request, cacheable_indices)

    def record(self, response_metadata: dict[str, Any]) -> None:
        """Parse cache token counts from provider response and update metrics + state."""
        call_metrics = self._provider.extract_cache_stats(response_metadata)
        self._metrics.merge(call_metrics)
        self._state.record_tokens(call_metrics.cached_input_tokens, call_metrics.cache_read_tokens)

        if _DEBUG and (call_metrics.cached_input_tokens or call_metrics.cache_read_tokens):
            logger.debug(
                "[cache] response: creation_tokens=%d read_tokens=%d est_savings=$%.4f",
                call_metrics.cached_input_tokens,
                call_metrics.cache_read_tokens,
                call_metrics.estimated_cost_savings_usd,
            )

    def get_metrics(self) -> CacheMetrics:
        """Return a snapshot copy of accumulated metrics."""
        from dataclasses import replace

        return replace(self._metrics)

    def reset(self) -> None:
        """Reset metrics and fingerprint state (e.g., on workspace/branch change)."""
        self._state.invalidate("manager.reset()")
        self._metrics = CacheMetrics()

    def invalidate(self, reason: str = "explicit") -> None:
        """Invalidate fingerprint state without resetting metrics."""
        self._state.invalidate(reason)

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_system(request: Any) -> str:
        for msg in request.messages:
            if msg.get("role") == "system":
                return msg.get("content", "")
        return ""

    @staticmethod
    def _extract_first_user(request: Any) -> str:
        for msg in request.messages:
            if msg.get("role") == "user":
                return msg.get("content", "")
        return ""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

# Module-level singletons so each provider_id gets one reused manager per process.
# This preserves fingerprint history across multiple agent deliberations in a run.
_managers: dict[str, ContextCacheManager] = {}


def make_cache_manager(provider_id: str) -> ContextCacheManager:
    """Return a ContextCacheManager wired for *provider_id*.

    Returns a cached singleton per provider_id so fingerprint state is
    preserved across multiple calls within the same process lifetime.
    """
    if provider_id not in _managers:
        if provider_id == "anthropic":
            cache_provider: ContextCacheProvider = AnthropicPromptCacheProvider()
        else:
            cache_provider = NoOpCacheProvider()
        _managers[provider_id] = ContextCacheManager(cache_provider)
    return _managers[provider_id]


def reset_all_managers() -> None:
    """Reset all cached manager singletons. Used in tests and on workspace change."""
    for manager in _managers.values():
        manager.reset()
    _managers.clear()
