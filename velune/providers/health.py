"""Internet connectivity detection with TTL-based caching."""

from __future__ import annotations

import logging
import socket
import time

logger = logging.getLogger("velune.providers.health")

_DEFAULT_TTL = 30.0  # seconds
_CHECK_HOST = "1.1.1.1"
_CHECK_PORT = 80
_CHECK_TIMEOUT = 2.0  # seconds


class InternetConnectivityChecker:
    """Checks internet connectivity via a TCP probe to 1.1.1.1:80.

    Results are cached for *ttl* seconds to avoid hammering the network on
    every router decision.
    """

    def __init__(self, ttl: float = _DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._cached: bool | None = None
        self._cached_at: float = 0.0

    def _probe(self) -> bool:
        """Attempt a TCP connection. Returns True if reachable within timeout."""
        try:
            with socket.create_connection((_CHECK_HOST, _CHECK_PORT), timeout=_CHECK_TIMEOUT):
                return True
        except OSError:
            return False

    @property
    def is_online(self) -> bool:
        """True if the internet is reachable; cached for *ttl* seconds."""
        now = time.monotonic()
        if self._cached is None or (now - self._cached_at) >= self._ttl:
            self._cached = self._probe()
            self._cached_at = now
            if not self._cached:
                logger.debug("Internet connectivity check: OFFLINE")
        return self._cached

    def invalidate(self) -> None:
        """Force the next call to is_online to re-probe."""
        self._cached = None


# ---------------------------------------------------------------------------
# Module-level singleton — shared across the process
# ---------------------------------------------------------------------------

_checker = InternetConnectivityChecker()


def is_online() -> bool:
    """Return True if the internet appears reachable (cached 30 s)."""
    return _checker.is_online


def get_checker() -> InternetConnectivityChecker:
    """Return the shared InternetConnectivityChecker instance."""
    return _checker
