"""API environments and ``{{variable}}`` interpolation (VAIR A3).

An environment names one deployment of an API — ``development`` at
``http://localhost:5000``, ``staging`` at ``https://staging.example.com`` — and
carries the values requests are written against::

    [resources.http.environments.development]
    base_url = "http://localhost:5000"
    variables = { api_version = "v1" }
    secret_variables = { admin_token = "aidly-dev-admin" }   # reference names, not values
    auth = "aidly-dev-token"                                  # a credential reference

A request can then say ``{{base_url}}/{{api_version}}/users`` and send
``Authorization`` from the environment's credential without the model ever
holding a secret.

Rules, all enforced here:

* A reference to an **unknown variable is an error** (never silently empty), and
  the error lists the available names — not their values.
* Variable values are literal. A value containing ``{{…}}`` is rejected, so
  interpolation cannot recurse or be steered by data.
* **Secret variables hold credential references.** The value is fetched from the
  encrypted keystore only when a request is sent and is registered for
  redaction. A request that interpolated a secret may only go to the
  environment's own origin (or an ``extra_origins`` entry), so switching
  environments or editing a URL cannot carry one environment's secret to another
  host.
* Nothing model-visible ever contains a secret value: listings show variable
  *names*, and the text of a request keeps ``{{admin_token}}`` as written.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from velune.resources.http.auth import CredentialError, secret_value, valid_reference
from velune.resources.http.netpolicy import Origin, PolicyViolation, parse_origin, parse_url

VAR_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]{0,63})\s*\}\}")
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
BUILTINS = ("base_url",)
MAX_VARIABLES = 100
MAX_VALUE_LENGTH = 4096

SecretLoader = Callable[[str], str]


class VariableError(ValueError):
    """A problem with an environment or a variable reference (safe to show)."""


def references(text: str) -> list[str]:
    """Variable names mentioned in *text*, in order, without duplicates."""
    seen: list[str] = []
    for match in VAR_RE.finditer(text):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


@dataclass(frozen=True, slots=True)
class Environment:
    name: str
    base_url: str
    origin: Origin
    variables: dict[str, str] = field(default_factory=dict)
    secret_variables: dict[str, str] = field(default_factory=dict)  # name -> credential reference
    auth: str | None = None
    production: bool = False
    extra_origins: tuple[Origin, ...] = ()
    verify_tls: bool = True
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def origins(self) -> frozenset[Origin]:
        """Every origin this environment's secrets may be sent to."""
        return frozenset({self.origin, *self.extra_origins})

    @classmethod
    def from_config(cls, name: str, config: Any) -> Environment:
        """Build and validate an environment from its configuration model or dict."""
        data = config if isinstance(config, Mapping) else config.model_dump()
        if not _ENV_NAME_RE.match(name):
            raise VariableError(f"'{name[:40]}' is not a valid environment name")
        try:
            base = parse_url(str(data.get("base_url", "")))
        except PolicyViolation as exc:
            raise VariableError(f"environment '{name}': base_url: {exc.message}") from None
        variables = dict(data.get("variables") or {})
        secrets = dict(data.get("secret_variables") or {})
        if len(variables) + len(secrets) > MAX_VARIABLES:
            raise VariableError(f"environment '{name}' has too many variables")
        for key, value in variables.items():
            _check_name(name, key)
            if key in BUILTINS:
                raise VariableError(f"environment '{name}': '{key}' is built in")
            if not isinstance(value, str) or len(value) > MAX_VALUE_LENGTH:
                raise VariableError(f"environment '{name}': variable '{key}' must be short text")
            if VAR_RE.search(value):
                raise VariableError(
                    f"environment '{name}': variable '{key}' contains '{{{{…}}}}'; "
                    "variable values are literal"
                )
        for key, ref in secrets.items():
            _check_name(name, key)
            if key in variables or key in BUILTINS:
                raise VariableError(f"environment '{name}': '{key}' is defined twice")
            if not valid_reference(ref):
                raise VariableError(
                    f"environment '{name}': secret variable '{key}' needs a credential reference"
                )
        auth = data.get("auth") or None
        if auth is not None and not valid_reference(auth):
            raise VariableError(f"environment '{name}': auth is not a valid credential reference")
        extra: list[Origin] = []
        for text in data.get("extra_origins") or []:
            try:
                extra.append(parse_origin(str(text)))
            except PolicyViolation as exc:
                raise VariableError(f"environment '{name}': extra_origins: {exc.message}") from None
        headers = {str(k): str(v) for k, v in (data.get("headers") or {}).items()}
        base_text = (
            f"{base.origin}{base.path.rstrip('/')}" if base.path != "/" else str(base.origin)
        )
        return cls(
            name=name,
            base_url=base_text,
            origin=base.origin,
            variables={str(k): v for k, v in variables.items()},
            secret_variables={str(k): v for k, v in secrets.items()},
            auth=auth,
            production=bool(data.get("production", False)),
            extra_origins=tuple(extra),
            verify_tls=bool(data.get("verify_tls", True)),
            headers=headers,
        )

    def summary(self) -> dict[str, Any]:
        """A model-safe description: variable *names* and non-secret values only."""
        return {
            "name": self.name,
            "base_url": self.base_url,
            "production": self.production,
            "variables": dict(sorted(self.variables.items())),
            "secret_variables": sorted(self.secret_variables),
            "authentication": "credential reference configured" if self.auth else "none",
            "verify_tls": self.verify_tls,
        }


def _check_name(env: str, key: str) -> None:
    if not isinstance(key, str) or not _NAME_RE.match(key):
        raise VariableError(f"environment '{env}': '{str(key)[:40]}' is not a valid variable name")


@dataclass(slots=True)
class Interpolated:
    text: str
    used_secret: bool = False


class VariableScope:
    """The variables a request may reference: built-ins, the environment, and extras."""

    def __init__(
        self,
        environment: Environment | None = None,
        extra: Mapping[str, str] | None = None,
        *,
        extra_secrets: frozenset[str] | set[str] = frozenset(),
        secret_loader: SecretLoader = secret_value,
    ) -> None:
        self.environment = environment
        self._extra = dict(extra or {})
        #: Names in *extra* whose values are secret (e.g. a token a collection
        #: extracted from a login response): they get the same origin rule.
        self._extra_secrets = frozenset(extra_secrets)
        self._load_secret = secret_loader

    def names(self) -> list[str]:
        names = list(self._extra)
        if self.environment is not None:
            names += [*BUILTINS, *self.environment.variables, *self.environment.secret_variables]
        return sorted(set(names))

    def interpolate(self, text: str) -> Interpolated:
        """Replace ``{{name}}`` with its value; unknown names raise :class:`VariableError`."""
        unknown = [n for n in references(text) if n not in self.names()]
        if unknown:
            available = ", ".join(self.names()) or "none (no environment selected)"
            raise VariableError(
                f"unknown variable(s): {', '.join(unknown[:5])}. Available: {available}"
            )
        used_secret = False

        def lookup(match: re.Match[str]) -> str:
            nonlocal used_secret
            name = match.group(1)
            if name in self._extra:
                if name in self._extra_secrets:
                    used_secret = True
                return self._extra[name]
            env = self.environment
            assert env is not None
            if name == "base_url":
                return env.base_url
            if name in env.variables:
                return env.variables[name]
            used_secret = True
            try:
                return self._load_secret(env.secret_variables[name])
            except CredentialError as exc:
                raise VariableError(f"secret variable '{name}': {exc}") from None

        return Interpolated(VAR_RE.sub(lookup, text), used_secret)
