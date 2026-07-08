"""Encrypted credential storage for resource connectors.

Reuses the existing provider keystore (:mod:`velune.providers.keystore`) rather
than introducing a second secret store or a second crypto path. Connector
configuration — which can include a database password or a Supabase service-role
key — is serialized to JSON and persisted under a namespaced id
(``resource:<type>:<name>``) through the same AES-GCM-encrypted
``credentials.json`` the providers use.

Guarantees:
- Nothing here writes plaintext to disk; every value goes through
  :func:`velune.providers.keystore.save_key`, which encrypts.
- The namespaced ids never collide with real provider ids (they contain a
  ``:``), and they are absent from ``PROVIDER_ENV_VARS`` so an unrelated env var
  can never be mistaken for a resource secret.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from velune.providers.keystore import delete_key, get_key, save_key

logger = logging.getLogger("velune.resources.secrets")

_NAMESPACE = "resource"

# Fields that must never be echoed back in status/preview output.
_SECRET_FIELDS: frozenset[str] = frozenset(
    {"password", "service_role_key", "service_key", "anon_key", "secret", "token"}
)


def _storage_id(resource_type: str, name: str) -> str:
    return f"{_NAMESPACE}:{resource_type}:{name}"


def save_resource_secret(resource_type: str, name: str, config: dict[str, Any]) -> None:
    """Encrypt and persist *config* for a named connector instance.

    The whole config dict (including any password) is JSON-serialized and stored
    as a single encrypted blob. Never logs the value.
    """
    payload = json.dumps(config, separators=(",", ":"))
    save_key(_storage_id(resource_type, name), payload)
    logger.debug("Stored encrypted config for %s:%s (%d fields)", resource_type, name, len(config))


def load_resource_secret(resource_type: str, name: str) -> dict[str, Any] | None:
    """Return the decrypted config for a named connector instance, or None."""
    raw = get_key(_storage_id(resource_type, name))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Stored config for %s:%s is corrupt; ignoring", resource_type, name)
        return None
    return data if isinstance(data, dict) else None


def delete_resource_secret(resource_type: str, name: str) -> None:
    """Remove the stored config for a named connector instance."""
    delete_key(_storage_id(resource_type, name))


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *config* with every secret field masked.

    Use this anywhere a config is rendered to a table, a log, or an approval
    preview so a password can never leak to a display sink.
    """
    redacted: dict[str, Any] = {}
    for key, value in config.items():
        if key.lower() in _SECRET_FIELDS and value:
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted
