"""A narrowly scoped secret scrubber for the acting path.

``redact_secrets`` (``velune.core.redaction``) already removes well-known key *shapes* and the values of a
fixed list of environment variables. The acting path additionally scrubs the **exact values** of the
keys this process actually holds: every provider environment variable and every key in the credential
store. It is applied to provider-bound request text, model text, tool output and anything that could
become an artifact or a diagnostic.

What this is not: it is pattern and value matching. It cannot find a secret it has no pattern or stored
value for, and it does not undo an encoding it does not know (base64, a split or re-spelled key, a
secret hashed or embedded in a larger token). It reduces exposure; it does not guarantee removal.

Importing this module loads no provider code. The credential store is imported lazily, only when a
credential source is actually called.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from urllib.parse import quote

from velune.core.redaction import REDACTION_PLACEHOLDER, redact_secrets

MIN_SECRET_CHARS = 8
Source = Callable[[], Iterable[str]]


def static_source(*values: str) -> Source:
    """A fixed set of secret values (tests, or values a caller already holds)."""
    return lambda: values


def environment_source(names: Iterable[str] | Callable[[], Iterable[str]]) -> Source:
    """The current values of the named environment variables, read at scrub time."""

    def read() -> list[str]:
        listed = names() if callable(names) else names
        return [value for name in listed if (value := os.environ.get(name, "").strip())]

    return read


def credential_store_source() -> Source:
    """Every key in the credential store. Read at scrub time so a rotated key is covered."""

    def read() -> list[str]:
        from velune.providers.keystore import CredentialManager

        records = CredentialManager().get_all_providers()
        return [
            key
            for record in records.values()
            if isinstance(record, dict) and isinstance(key := record.get("key"), str)
        ]

    return read


def provider_environment_source() -> Source:
    """Every provider API-key environment variable the keystore knows about."""

    def names() -> list[str]:
        from velune.providers.keystore import all_provider_env_vars

        return all_provider_env_vars()

    return environment_source(names)


class SecretScrubber:
    """Replaces known secret values (and common spellings of them) and known key shapes.

    A source that raises is skipped; scrubbing never fails open on the patterns, and never raises.
    """

    def __init__(self, *sources: Source) -> None:
        self._sources = sources

    def _values(self) -> list[str]:
        found: set[str] = set()
        for source in self._sources:
            try:
                values = list(source())
            except Exception:  # a locked or unreadable store must not break scrubbing
                continue
            for value in values:
                if not isinstance(value, str) or len(value.strip()) < MIN_SECRET_CHARS:
                    continue
                value = value.strip()
                found.add(value)
                found.add(json.dumps(value)[1:-1])  # as it would appear inside JSON text
                found.add(quote(value, safe=""))  # as it would appear in a URL
        return sorted(
            found, key=len, reverse=True
        )  # longest first, so a prefix cannot leave a tail

    def scrub(self, text: str) -> str:
        if not text:
            return text
        for value in self._values():
            if value in text:
                text = text.replace(value, REDACTION_PLACEHOLDER)
        return redact_secrets(text)

    __call__ = scrub

    def contains_secret(self, text: str) -> bool:
        return bool(text) and self.scrub(text) != text


def default_scrubber() -> SecretScrubber:
    """Provider env vars plus the credential store, on top of the shared key-shape patterns."""
    return SecretScrubber(provider_environment_source(), credential_store_source())


__all__ = [
    "MIN_SECRET_CHARS",
    "SecretScrubber",
    "credential_store_source",
    "default_scrubber",
    "environment_source",
    "provider_environment_source",
    "static_source",
]
