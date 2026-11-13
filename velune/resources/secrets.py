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
    """Return the decrypted config for a named connector instance, or None.

    Every secret field of the returned config is registered with the redaction
    layer, so the value is masked in tool results, logs and previews even when
    it has no recognizable shape.
    """
    raw = get_key(_storage_id(resource_type, name))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Stored config for %s:%s is corrupt; ignoring", resource_type, name)
        return None
    if not isinstance(data, dict):
        return None
    register_config_secrets(data)
    return data


def register_config_secrets(config: dict[str, Any]) -> None:
    """Register every secret value in *config* (and any URL password) for redaction."""
    from urllib.parse import urlparse

    from velune.core.redaction import is_sensitive_key, register_secret_value

    for key, value in config.items():
        if not isinstance(value, str) or not value:
            continue
        if key.lower() in _SECRET_FIELDS or is_sensitive_key(key):
            register_secret_value(value)
        elif "://" in value:
            try:
                password = urlparse(value).password
            except ValueError:
                password = None
            if password:
                register_secret_value(password)


def delete_resource_secret(resource_type: str, name: str) -> None:
    """Remove the stored config for a named connector instance."""
    delete_key(_storage_id(resource_type, name))


def redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *config* with every secret field masked.

    Use this anywhere a config is rendered to a table, a log, or an approval
    preview so a password can never leak to a display sink.
    """
    from velune.core.redaction import is_sensitive_key

    redacted: dict[str, Any] = {}
    for key, value in config.items():
        if (key.lower() in _SECRET_FIELDS or is_sensitive_key(key)) and value:
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted
