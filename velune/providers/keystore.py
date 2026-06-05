"""BYOK key storage backed by the OS keyring with env-var fallback."""

from __future__ import annotations

import os

import keyring
import keyring.errors

_SERVICE = "velune/{}"
_USERNAME = "api_key"

_ENV_VARS: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "xai": "XAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "huggingface": "HF_TOKEN",
}


def save_key(provider_id: str, api_key: str) -> None:
    """Persist *api_key* for *provider_id* in the OS keyring."""
    keyring.set_password(_SERVICE.format(provider_id), _USERNAME, api_key)


def get_key(provider_id: str) -> str | None:
    """Return the API key for *provider_id*.

    Lookup order:
    1. OS keyring (``velune/{provider_id}`` / ``api_key``)
    2. Environment variable mapped in ``_ENV_VARS``
    3. ``None``
    """
    try:
        key = keyring.get_password(_SERVICE.format(provider_id), _USERNAME)
        if key:
            return key
    except Exception:
        pass

    env_var = _ENV_VARS.get(provider_id)
    if env_var:
        return os.getenv(env_var) or None

    return None


def delete_key(provider_id: str) -> None:
    """Remove the stored key for *provider_id* from the OS keyring."""
    try:
        keyring.delete_password(_SERVICE.format(provider_id), _USERNAME)
    except keyring.errors.PasswordDeleteError:
        pass


def has_key(provider_id: str) -> bool:
    """Return True if a key is available for *provider_id*."""
    return get_key(provider_id) is not None
