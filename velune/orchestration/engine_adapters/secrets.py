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


class ScrubberUnavailable(Exception):
    """A required secret source could not be read. The message never contains a value."""

    def __init__(self) -> None:
        super().__init__("the credential-aware secret scrubber is unavailable")


class SecretScrubber:
    """Replaces known secret values (and common spellings of them) and known key shapes.

    By default a source that raises is skipped, so scrubbing never raises and falls back to the key
    shapes. With ``strict=True`` that downgrade is not allowed: a source that raises makes
    :meth:`scrub` raise :class:`ScrubberUnavailable`, which every acting-path consumer turns into a
    refusal or withheld text rather than passing content through with weaker redaction.
    """

    def __init__(self, *sources: Source, strict: bool = False) -> None:
        self._sources = sources
        self._strict = strict

    def verify(self) -> None:
        """Read every source once; raise :class:`ScrubberUnavailable` if any cannot be read."""
        for source in self._sources:
            try:
                list(source())
            except Exception:
                raise ScrubberUnavailable() from None

    def _values(self) -> list[str]:
        found: set[str] = set()
        for source in self._sources:
            try:
                values = list(source())
            except Exception:  # a locked or unreadable store
                if self._strict:
                    raise ScrubberUnavailable() from None  # never the underlying error or values
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


def credential_scrubber() -> SecretScrubber:
    """The scrubber the native acting entrypoint uses: strict, and verified before it is returned.

    Reuses the existing keystore and credential manager. Raises :class:`ScrubberUnavailable` if the
    provider environment or the credential store cannot be read, so a caller never silently proceeds
    with weaker redaction. Pattern and exact-value matching only: it cannot find a secret that is
    encoded (base64, split, hashed, embedded in a larger token), is shorter than ``MIN_SECRET_CHARS``,
    or is not held by this process.
    """
    scrubber = SecretScrubber(provider_environment_source(), credential_store_source(), strict=True)
    scrubber.verify()
    return scrubber


__all__ = [
    "MIN_SECRET_CHARS",
    "ScrubberUnavailable",
    "SecretScrubber",
    "credential_scrubber",
    "credential_store_source",
    "default_scrubber",
    "environment_source",
    "provider_environment_source",
    "static_source",
]
