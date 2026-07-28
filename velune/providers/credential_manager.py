"""Single entry point for adding a provider credential.

Before this module, three call sites each hand-rolled their own
validate → save_key → mark_verified sequence, with subtly different
behavior:

* ``cli/provider_ui.py``'s ``ProviderPalette._connect`` (the REPL's
  ``/providers add`` / ``/connect``) — validates, saves, marks verified with
  a model count, and offers a "save anyway" unverified path on failure.
* ``cli/commands/providers.py``'s ``add_provider`` (``velune provider add``)
  — the same shape, plus auto-adopting the first-added provider as the
  workspace default (a behavior the REPL path never had).
* ``cli/onboarding/stages.py``'s ``_configure_one_provider_key`` (the
  first-run wizard) — validates and saves, but never called ``mark_verified``
  with a model count and never offered "save anyway".

This module is the one place that sequence lives now. All three UIs call
:func:`add_credential` (or its sync wrapper) and differ only in how they
render the structured :class:`CredentialAddResult` — a widget-driven REPL
screen, a plain ``rich`` prompt, or a wizard transient message.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from velune.providers import catalog
from velune.providers.default_provider import set_first_default
from velune.providers.keystore import mark_verified, save_key
from velune.providers.validation import ValidationResult, validate_provider


@dataclass(slots=True)
class CredentialAddResult:
    """Outcome of :func:`add_credential`, for a caller to render.

    ``ok`` is True only when the provider accepted the key (or validation was
    explicitly skipped). A key saved unverified after a failed validation
    (the REPL's "save anyway" / the CLI's "save anyway, network may be
    offline" paths) is recorded with ``saved=True, ok=False`` — it *is*
    stored, but the caller should still show it as unverified, not as a
    success.
    """

    ok: bool
    provider_id: str
    saved: bool
    verified: bool
    became_default: bool
    validation: ValidationResult | None = None
    unknown_provider: bool = False
    not_a_key_provider: bool = False


def persist_credential(
    provider_id: str,
    key: str,
    *,
    verified: bool,
    model_count: int = 0,
    set_as_first_default: bool = False,
    config_path: Path | None = None,
) -> bool:
    """Persist *key* directly, with an already-known verdict.

    For a caller that already ran (or deliberately skipped) validation itself
    — e.g. the REPL's "save anyway" branch, re-saving a key that just failed
    validation as unverified without re-running that validation call a
    second time. :func:`add_credential` calls this internally too; this is
    the one place ``save_key``/``mark_verified``/``set_first_default`` are
    actually invoked from.

    Returns whether this call set the workspace default provider.
    """
    save_key(provider_id, key, verified=verified)
    if verified:
        mark_verified(provider_id, model_count=model_count)
    if not set_as_first_default:
        return False
    return set_first_default(provider_id, config_path=config_path)


async def add_credential(
    provider_id: str,
    key: str,
    *,
    skip_validation: bool = False,
    save_unverified_on_failure: bool = False,
    set_as_first_default: bool = True,
    config_path: Path | None = None,
) -> CredentialAddResult:
    """Validate (unless skipped) and persist *key* for *provider_id*.

    Does not prompt, print, or render anything — every caller owns its own
    UI. *config_path*, if given, is forwarded to
    :func:`~velune.providers.default_provider.set_first_default` for callers
    (the REPL) that know their workspace's ``velune.toml`` path explicitly
    rather than resolving it by walking up from ``cwd``.
    """
    meta = catalog.get(provider_id)
    if meta is None:
        return CredentialAddResult(
            ok=False,
            provider_id=provider_id,
            saved=False,
            verified=False,
            became_default=False,
            unknown_provider=True,
        )
    if not meta.requires_key:
        return CredentialAddResult(
            ok=False,
            provider_id=provider_id,
            saved=False,
            verified=False,
            became_default=False,
            not_a_key_provider=True,
        )

    if skip_validation:
        became_default = persist_credential(
            provider_id,
            key,
            verified=False,
            set_as_first_default=set_as_first_default,
            config_path=config_path,
        )
        return CredentialAddResult(
            ok=True,
            provider_id=provider_id,
            saved=True,
            verified=False,
            became_default=became_default,
        )

    result = await validate_provider(provider_id, key)

    if result.ok:
        became_default = persist_credential(
            provider_id,
            key,
            verified=True,
            model_count=len(result.models),
            set_as_first_default=set_as_first_default,
            config_path=config_path,
        )
        return CredentialAddResult(
            ok=True,
            provider_id=provider_id,
            saved=True,
            verified=True,
            became_default=became_default,
            validation=result,
        )

    if save_unverified_on_failure:
        became_default = persist_credential(
            provider_id,
            key,
            verified=False,
            set_as_first_default=set_as_first_default,
            config_path=config_path,
        )
        return CredentialAddResult(
            ok=False,
            provider_id=provider_id,
            saved=True,
            verified=False,
            became_default=became_default,
            validation=result,
        )

    return CredentialAddResult(
        ok=False,
        provider_id=provider_id,
        saved=False,
        verified=False,
        became_default=False,
        validation=result,
    )


def add_credential_sync(
    provider_id: str,
    key: str,
    *,
    skip_validation: bool = False,
    save_unverified_on_failure: bool = False,
    set_as_first_default: bool = True,
    config_path: Path | None = None,
) -> CredentialAddResult:
    """Synchronous wrapper around :func:`add_credential` for non-async contexts
    (the Typer CLI), mirroring ``validation.py``'s ``validate_provider_sync``.
    """
    coro = add_credential(
        provider_id,
        key,
        skip_validation=skip_validation,
        save_unverified_on_failure=save_unverified_on_failure,
        set_as_first_default=set_as_first_default,
        config_path=config_path,
    )
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=20.0)
        return loop.run_until_complete(coro)
    except RuntimeError:
        # No event loop in this thread at all (typical Typer/CLI entry point).
        return asyncio.run(coro)
