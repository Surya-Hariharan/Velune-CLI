"""BYOK key storage backed by the OS keyring with env-var fallback.

Performance notes
-----------------
On Windows the OS keyring is the Windows Credential Manager (DPAPI). A single
``keyring.get_password`` call can block for *seconds* the first time it is hit
after a cold boot or screen unlock, and Velune historically issued one such
call per provider (9+) sequentially during startup — tens of seconds of pure
blocking. This module now:

* imports ``keyring`` lazily (env-only users never pay the import cost);
* checks the free, instant environment variables before ever touching the
  keyring;
* parallelizes the remaining keyring probes across a thread pool.

We deliberately do *not* cache resolved keys process-wide: a stale cache would
mask a key the user just saved (or deleted) mid-session, and correctness here
matters more than shaving a second off repeated lookups.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

_SERVICE = "velune/{}"
_USERNAME = "api_key"

PROVIDER_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "xai": "XAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "huggingface": "HF_TOKEN",
    "together": "TOGETHER_API_KEY",
    "fireworks": "FIREWORKS_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "cohere": "COHERE_API_KEY",
    "nvidia": "NVIDIA_API_KEY",
}

_ENV_VARS = PROVIDER_ENV_VARS


def _keyring_get(provider_id: str) -> str | None:
    """Raw, uncached OS-keyring lookup. Lazily imports keyring."""
    try:
        import keyring

        return keyring.get_password(_SERVICE.format(provider_id), _USERNAME)
    except Exception:
        return None


def _resolve_uncached(provider_id: str) -> str | None:
    """Resolve a key without consulting the cache (keyring then env)."""
    key = _keyring_get(provider_id)
    if key:
        return key
    env_var = _ENV_VARS.get(provider_id)
    if env_var:
        return os.getenv(env_var) or None
    return None


def save_key(provider_id: str, api_key: str) -> None:
    """Persist *api_key* for *provider_id* in the OS keyring."""
    import keyring

    keyring.set_password(_SERVICE.format(provider_id), _USERNAME, api_key)


def get_key(provider_id: str) -> str | None:
    """Return the API key for *provider_id* (OS keyring first, then env var)."""
    return _resolve_uncached(provider_id)


def delete_key(provider_id: str) -> None:
    """Remove the stored key for *provider_id* from the OS keyring."""
    import keyring
    import keyring.errors

    try:
        keyring.delete_password(_SERVICE.format(provider_id), _USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass


def has_key(provider_id: str) -> bool:
    """Return True if a key is available for *provider_id*."""
    return get_key(provider_id) is not None


def is_ollama_live(timeout: float = 0.25) -> bool:
    """Return True if a local Ollama server is reachable.

    Uses a raw TCP connect to ``127.0.0.1:11434`` rather than an HTTP GET:

    * It avoids importing ``httpx`` on the hot startup path.
    * It targets ``127.0.0.1`` explicitly. Resolving ``localhost`` makes the
      client try IPv6 (``::1``) first, and when nothing is listening that
      attempt burns the *full* timeout before falling back to IPv4 — doubling
      the cost for the common "Ollama not running" case.
    * A closed port refuses the connection immediately, so when Ollama is down
      this returns in microseconds instead of blocking for seconds.
    """
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=timeout):
            return True
    except OSError:
        return False


def list_configured_providers(include_ollama: bool = True) -> list[str]:
    """Return provider IDs that have a usable key, resolved in parallel.

    Environment variables are checked first (free, instant). Only providers
    not already satisfied by an env var incur a (parallelized, cached) keyring
    probe. Ollama is local and keyless — it counts as configured when its
    server is reachable.
    """
    configured: set[str] = set()
    needs_keyring: list[str] = []

    for pid, env_var in _ENV_VARS.items():
        if os.getenv(env_var):
            configured.add(pid)
        else:
            needs_keyring.append(pid)

    if needs_keyring:
        with ThreadPoolExecutor(max_workers=min(8, len(needs_keyring))) as pool:
            futures = {pool.submit(_keyring_get, pid): pid for pid in needs_keyring}
            for fut in as_completed(futures):
                pid = futures[fut]
                try:
                    key = fut.result()
                except Exception:
                    key = None
                if key:
                    configured.add(pid)

    # Preserve a stable, declaration-order result.
    ordered = [pid for pid in _ENV_VARS if pid in configured]

    if include_ollama and is_ollama_live():
        ordered.insert(0, "ollama")
    return ordered
