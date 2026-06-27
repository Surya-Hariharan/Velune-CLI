"""Deterministic SHA-256 fingerprinting for context segments."""

from __future__ import annotations

import hashlib


class ContextFingerprinter:
    """Generates short deterministic hex fingerprints for content strings.

    Used to detect whether a cached prefix is still valid without comparing
    the full content.
    """

    _HEX_LENGTH = 16  # 64-bit prefix of SHA-256 — collision-safe for this use case

    @classmethod
    def fingerprint(cls, content: str) -> str:
        """Return a 16-char hex fingerprint for *content*."""
        return hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[
            : cls._HEX_LENGTH
        ]

    @classmethod
    def fingerprint_segments(cls, segments: dict[str, str]) -> dict[str, str]:
        """Fingerprint a named map of segments.

        Returns {segment_name: hex_fingerprint}.
        Useful for debug output — shows all segment fingerprints at once.
        """
        return {name: cls.fingerprint(content) for name, content in segments.items()}
