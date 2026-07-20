"""Re-verification of stored provider credentials.

The keystore records *whether* a key was accepted and *when*; this module is
what re-asks the provider once that answer goes stale, and writes the new
verdict back. It is the only place that turns a
:class:`~velune.providers.validation.ValidationResult` into a persisted
:class:`~velune.providers.keystore.KeyState`.

The central rule, and the reason this lives apart from ``validation.py``:

    Only a verdict that says something about the *key* may mark it invalid.

A ``NETWORK_ERROR`` or ``RATE_LIMITED`` response tells us about the network or
the provider's load, not about the credential — so those leave the record STALE
and it is retried later. Without that distinction, running re-verification in
the background would tell every offline user their keys are broken.
"""

from __future__ import annotations

import asyncio
import logging

from velune.providers.keystore import (
    KeyState,
    get_key,
    list_stale_providers,
    mark_invalid,
    mark_verified,
    verification_state,
)
from velune.providers.validation import (
    ValidationResult,
    ValidationStatus,
    validate_provider,
)

logger = logging.getLogger("velune.providers.verifier")

# Verdicts that are a statement about the credential itself. Anything outside
# this set (network error, rate limit, unknown) is treated as "no new
# information" and leaves the stored state untouched.
_KEY_REJECTING_STATUSES: frozenset[ValidationStatus] = frozenset(
    {
        ValidationStatus.INVALID_KEY,
        ValidationStatus.EXPIRED_KEY,
        ValidationStatus.REVOKED_KEY,
        ValidationStatus.MALFORMED_KEY,
        ValidationStatus.PERMISSION_DENIED,
    }
)

# Cap on how many providers we re-check at once, so a user with a dozen
# configured providers doesn't open a dozen simultaneous TLS connections at REPL
# start.
_MAX_CONCURRENCY = 4


async def reverify(provider_id: str) -> ValidationResult:
    """Re-validate *provider_id* against the live API and persist the verdict.

    Returns the raw :class:`ValidationResult` so callers can render detail. The
    stored state is updated only when the result is conclusive — see the module
    docstring.
    """
    key = get_key(provider_id) or ""
    result = await validate_provider(provider_id, key)

    if result.ok:
        mark_verified(provider_id, model_count=len(result.models))
    elif result.status in _KEY_REJECTING_STATUSES:
        mark_invalid(provider_id, reason=result.message)
    else:
        # Inconclusive (offline, rate-limited, provider outage). Leave the
        # record as-is so it stays STALE and is retried on the next run.
        logger.debug(
            "Re-verification of %s was inconclusive (%s); leaving state unchanged.",
            provider_id,
            result.status,
        )

    return result


async def reverify_stale(max_concurrency: int | None = None) -> list[ValidationResult]:
    """Re-verify every provider whose key has aged past the TTL.

    Safe to fire and forget on startup: it touches only providers already in
    :attr:`KeyState.STALE`, never blocks on more than *max_concurrency*
    (default ``_MAX_CONCURRENCY``) requests at once, and swallows per-provider
    failures so one dead endpoint can't take down the sweep.
    """
    stale = list_stale_providers()
    if not stale:
        return []

    semaphore = asyncio.Semaphore(max_concurrency or _MAX_CONCURRENCY)

    async def _one(pid: str) -> ValidationResult | None:
        async with semaphore:
            try:
                return await reverify(pid)
            except Exception as exc:  # noqa: BLE001 - background sweep must not raise
                logger.debug("Re-verification of %s failed: %s", pid, exc)
                return None

    settled = await asyncio.gather(*(_one(pid) for pid in stale))
    results = [r for r in settled if r is not None]

    newly_invalid = [r.provider_id for r in results if r.status in _KEY_REJECTING_STATUSES]
    if newly_invalid:
        logger.info("Re-verification rejected key(s) for: %s", ", ".join(newly_invalid))

    return results


async def ensure_verified(provider_id: str) -> KeyState:
    """Re-verify *provider_id* if it is stale, then report its state.

    For a caller that is about to actually use the credential and would rather
    pay one round-trip than fail mid-request.
    """
    state = verification_state(provider_id)
    if state is KeyState.STALE:
        await reverify(provider_id)
        state = verification_state(provider_id)
    return state
