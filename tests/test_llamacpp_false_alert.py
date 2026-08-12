"""Regression: the ProactiveWatcher must not alert "Provider llamacpp
unavailable" for every user regardless of which provider they actually use.

Root cause traced end-to-end: LlamaCppProvider.initialize() unconditionally
raises ProviderConnectionError (the llama-cpp-python extra was permanently
removed — see adapters/llamacpp.py) so its health check can never pass
without a manual, deliberately-unsupported install. The health monitor
dutifully polls it anyway (it's keyless, so the earlier UNCONFIGURED-state
fix for key-requiring providers doesn't apply), and the watcher's
`_KEYLESS_PROVIDERS` allowlist previously treated that permanent UNAVAILABLE
as alert-worthy for every installation, unconditionally — producing the
"Provider llamacpp unavailable" warning while the user was actively using an
unrelated, correctly-configured Groq model.
"""

from __future__ import annotations

import asyncio

import pytest

from velune.proactive.watcher import _KEYLESS_PROVIDERS


def test_llamacpp_is_not_in_the_always_alert_set():
    """llamacpp can never pass its own health check without a manual, out-of-
    band unsafe-dependency install — it must not be treated as "configured by
    default" the way Ollama/LM Studio are."""
    assert "llamacpp" not in _KEYLESS_PROVIDERS


def test_openai_compat_is_not_in_the_always_alert_set():
    """A generic self-hosted-endpoint catch-all has no default port anyone is
    likely to have listening; alerting on it by default is the same "mistook
    unconfigured for unavailable" bug for a different provider."""
    assert "openai-compat" not in _KEYLESS_PROVIDERS


def test_ollama_and_lmstudio_remain_in_the_always_alert_set():
    """These two DO have widely-adopted default local ports — "might be
    running, alert if it goes down" stays a reasonable assumption for them."""
    assert "ollama" in _KEYLESS_PROVIDERS
    assert "lmstudio" in _KEYLESS_PROVIDERS


def test_llamacpp_can_never_pass_its_own_health_check():
    """Documents *why* the exclusion above is correct, not just that it is."""
    from velune.core.errors.provider import ProviderConnectionError
    from velune.providers.adapters.llamacpp import LlamaCppProvider

    provider = LlamaCppProvider()

    async def _try():
        with pytest.raises(ProviderConnectionError):
            await provider.initialize()

    asyncio.run(_try())
