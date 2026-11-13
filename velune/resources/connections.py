"""Named connections shared by the database integrations (VAIR A5).

PostgreSQL, MySQL and Supabase each serve any number of *named* connections
(``connection="staging"``). This mixin resolves a name to a :class:`Profile`:

* the endpoint and password of a connection are stored **together** in the encrypted
  keystore (``resource:<type>:<name>``, written by ``/resource configure``), so a
  repository's ``velune.toml`` can never redirect a stored password to another server;
* configuration (``[resources.<type>.connections.<name>]``) only *names* a credential and
  sets policy — ``read_only``, ``environment``, limits, column redaction;
* an endpoint given in configuration is honoured only for a trusted workspace and only
  when there is no stored credential (so there is no password to protect).

No new credential storage: it reads the existing keystore through
:mod:`velune.resources.secrets`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from velune.resources.base import ErrorCode, ResourceResult
from velune.resources.secrets import load_resource_secret, register_config_secrets
from velune.resources.sql_results import ResultLimits

CONNECTION_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,39}$")


@dataclass(slots=True)
class Profile:
    """One resolved connection. ``config`` holds secrets: never render it."""

    name: str
    config: dict[str, Any]
    environment: str = ""
    read_only: bool = False
    statement_timeout: float = 30.0
    connect_timeout: float = 5.0
    idle_transaction_timeout: float = 60.0
    limits: ResultLimits = field(default_factory=ResultLimits)
    remote: bool = False
    source: str = "keystore"

    @property
    def production(self) -> bool:
        return self.environment.strip().lower() in {"prod", "production", "live"}

    def summary(self) -> dict[str, Any]:
        """Secret-free description for display and for the model."""
        info: dict[str, Any] = {
            "name": self.name,
            "host": self.config.get("host") or "localhost",
            "port": self.config.get("port"),
            "database": self.config.get("database") or None,
            "read_only": self.read_only,
        }
        if self.environment:
            info["environment"] = self.environment
        if self.remote:
            info["remote"] = True
        return info


class NamedConnections:
    """Mixin: resolve ``connection`` names to :class:`Profile` objects.

    Expects ``resource_id``, ``name``, ``display_name``, ``settings``, ``_trusted`` and
    ``_explicit_config`` on the host class, and ``_is_remote`` / ``_connection_hint``
    overrides where the default does not fit.
    """

    resource_id: str
    name: str
    display_name: str
    settings: Any
    _trusted: bool
    _explicit_config: dict[str, Any] | None

    def _is_remote(self, stored: dict[str, Any]) -> bool:
        return False

    def _limits_for(self, entry: Any = None) -> ResultLimits:
        s = self.settings
        extra = tuple(s.redact_columns) + tuple(getattr(entry, "redact_columns", None) or ())
        return ResultLimits(
            max_rows=int(getattr(entry, "max_rows", None) or s.max_rows),
            max_cell_chars=int(s.max_cell_chars),
            max_chars=int(s.max_result_chars),
            redact_columns=extra,
        )

    def _stored_names(self) -> list[str]:
        prefix = f"resource:{self.resource_id}:"
        try:
            from velune.providers.keystore import CredentialManager

            ids = CredentialManager().get_all_providers().keys()
        except Exception:
            return []
        return sorted(i[len(prefix) :] for i in ids if isinstance(i, str) and i.startswith(prefix))

    def connection_names(self) -> list[str]:
        """Every connection this connector can open, default first."""
        names: list[str] = []
        if self._explicit_config:
            names.append(self.name)
        names.extend(self.settings.connections)
        names.extend(self._stored_names())
        seen: dict[str, None] = {}
        for n in names:
            if CONNECTION_NAME.match(n):
                seen.setdefault(n, None)
        ordered = list(seen)
        default = self._default_name(ordered)
        if default in ordered:
            ordered.remove(default)
            ordered.insert(0, default)
        return ordered

    def _default_name(self, names: list[str]) -> str | None:
        if self.settings.default_connection and self.settings.default_connection in names:
            return self.settings.default_connection
        if self.name in names:
            return self.name
        if len(names) == 1:
            return names[0]
        return None

    def _build_profile(self, name: str) -> tuple[Profile | None, str]:
        """Resolve *name* to a profile. ``(None, reason)`` when it cannot be used."""
        entry = self.settings.connections.get(name)
        stored: dict[str, Any] | None = None
        if self._explicit_config and name == self.name:
            register_config_secrets(self._explicit_config)
            stored = dict(self._explicit_config)
            source = "explicit"
        else:
            # Naming a *different* stored credential is a way to reuse a secret, so only a
            # trusted workspace's configuration may do it.
            alias = getattr(entry, "credential", None) if entry and self._trusted else None
            stored = load_resource_secret(self.resource_id, alias or name)
            source = "keystore"
        if stored is None and entry is not None and self._trusted and entry.host:
            # No stored credential means no password to protect: a configured
            # endpoint (trusted workspaces only) is allowed, without secrets.
            stored = {
                k: v
                for k, v in {
                    "host": entry.host,
                    "port": entry.port,
                    "database": entry.database,
                    "username": entry.username,
                }.items()
                if v
            }
            source = "config"
        if not stored:
            return None, (
                f"No stored configuration for {self.display_name} '{name}'. "
                f"Run /resource configure {self.resource_id} {name if name != self.resource_id else ''}"
            ).strip()
        remote = self._is_remote(stored)
        s = self.settings
        profile = Profile(
            name=name,
            config=stored,
            # The label the user stored with the credential wins: configuration can add a
            # label to an unlabelled connection but never relabel a production one.
            environment=str(stored.get("environment") or getattr(entry, "environment", "") or ""),
            read_only=bool(getattr(entry, "read_only", False) or stored.get("read_only")),
            statement_timeout=float(
                getattr(entry, "statement_timeout", None) or s.statement_timeout
            ),
            connect_timeout=float(s.connect_timeout),
            idle_transaction_timeout=float(s.idle_transaction_timeout),
            limits=self._limits_for(entry),
            remote=remote,
            source=source,
        )
        return profile, ""

    def _profile_for(self, name: str | None) -> tuple[Profile | None, ResourceResult | None]:
        names = self.connection_names()
        if name is None or name == "":
            name = self._default_name(names)
            if name is None:
                if not names:
                    return None, ResourceResult.failure(
                        f"{self.display_name} is not configured — the user must run "
                        f"/resource configure {self.resource_id}.",
                        code=ErrorCode.UNAVAILABLE,
                    )
                return None, ResourceResult.failure(
                    f"Several connections are configured ({', '.join(names)}); "
                    "name one with the 'connection' argument.",
                    code=ErrorCode.VALIDATION_FAILED,
                )
        if not CONNECTION_NAME.match(str(name)) or name not in names:
            return None, ResourceResult.failure(
                f"Unknown connection '{str(name)[:40]}'. Configured: {', '.join(names) or 'none'}.",
                code=ErrorCode.NOT_FOUND,
            )
        profile, reason = self._build_profile(name)
        if profile is None:
            return None, ResourceResult.failure(reason, code=ErrorCode.UNAVAILABLE)
        return profile, None

    def _resolve_config(self) -> dict[str, Any] | None:
        """The default connection's driver config (secrets registered for redaction)."""
        names = self.connection_names()
        default = self._default_name(names)
        if default is None:
            return None
        profile, _ = self._build_profile(default)
        return profile.config if profile else None


def _coerce_settings(settings: Any) -> Any:
    from velune.kernel.config import DatabaseConfig

    if isinstance(settings, DatabaseConfig):
        return settings
    if isinstance(settings, dict):
        try:
            return DatabaseConfig.model_validate(settings)
        except Exception:
            return DatabaseConfig()
    return DatabaseConfig()


def _workspace_trusted(workspace: Any, explicit: bool | None) -> bool:
    if explicit is not None:
        return explicit
    if workspace is None:
        return True
    try:
        from velune.core.trust import is_trusted

        return bool(is_trusted(workspace))
    except Exception:
        return False
