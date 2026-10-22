"""Re-verification of stored provider credentials.

The keystore records *whether* a key was accepted and *when*; this module is
what re-asks the provider once that answer goes stale, and writes the new
verdict back. It is the only place that turns a
:class:`~velune.providers.validation.ValidationResult` into a persisted
:class:`~velune.providers.keystore.KeyState`.

The central rule, and the reason this lives apart from ``validation.py``:

    Only a verdict that says something about the *key* may mark it invalid.

A ``NETWORK_ERROR``, ``TIMEOUT``, ``RATE_LIMITED`` or ``PROVIDER_UNAVAILABLE``
response tells us about the network or the provider's load, not about the
credential — so those leave the record as it was (with the reason noted) and
it is retried later. ``PERMISSION_DENIED`` is a 403 on one endpoint for a key
that *did* authenticate, and ``MALFORMED_KEY`` is our own pre-check, not the
provider's verdict; neither may mark a key invalid either. Without that
distinction, running re-verification in the background would tell every
offline or restricted-key user their keys are broken.
"""

from __future__ import annotations

import asyncio
import logging

from velune.providers.keystore import (
    KeyState,
    get_key,
    key_source,
    list_recheck_due_providers,
    mark_invalid,
    mark_verified,
    note_check_failure,
    verification_state,
)
from velune.providers.validation import (
    KEY_REJECTING_STATUSES,
    ValidationResult,
    ValidationStatus,
    validate_provider,
)

logger = logging.getLogger("velune.providers.verifier")

# Verdicts that are a statement about the credential itself. Anything outside
# this set is treated as "no new information" and leaves the stored state
# untouched. Kept under its old name for callers that import it.
_KEY_REJECTING_STATUSES: frozenset[ValidationStatus] = KEY_REJECTING_STATUSES

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

    if key_source(provider_id).source != "stored":
        # The key in effect came from the environment: its verdict must not be
        # written onto the (different, or absent) stored record.
        return result

    if result.ok:
        mark_verified(provider_id, model_count=len(result.models))
    elif result.status in _KEY_REJECTING_STATUSES:
        mark_invalid(provider_id, reason=result.message)
    else:
        # Inconclusive (offline, timeout, rate limit, outage, restricted key).
        # Keep the state, note why, and retry on a later run.
        note_check_failure(provider_id, reason=result.message)
        logger.debug(
            "Re-verification of %s was inconclusive (%s); leaving state unchanged.",
            provider_id,
            result.status,
        )

    return result


async def reverify_stale(max_concurrency: int | None = None) -> list[ValidationResult]:
    """Re-verify every provider whose key has aged past the TTL.

    Also re-checks keys marked INVALID once ``INVALID_RECHECK_SECONDS`` has
    passed, so a recoverable rejection (and INVALID marks written by older
    versions for 403s) heals without removing and re-adding the key.

    Safe to fire and forget on startup: it touches only providers in
    :attr:`KeyState.STALE` or due INVALID ones, never blocks on more than *max_concurrency*
    (default ``_MAX_CONCURRENCY``) requests at once, and swallows per-provider
    failures so one dead endpoint can't take down the sweep.
    """
    stale = list_recheck_due_providers()
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
