"""Route every ``httpx.AsyncClient`` built during a test to an in-process handler.

Validators, discoverers and adapters construct their own clients, so patching a
single client instance is not enough. ``install_mock_transport`` swaps the
transport on every client created while the test runs; nothing can reach a real
provider, and the handler sees exactly the requests that would have been sent.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

Handler = Callable[[httpx.Request], httpx.Response]

# Captured once, so installing a second handler in the same test replaces the
# first instead of wrapping it.
_ORIGINAL_INIT = httpx.AsyncClient.__init__


def install_mock_transport(monkeypatch, handler: Handler) -> list[httpx.Request]:
    """Patch ``httpx.AsyncClient`` so all requests go to *handler*; return the request log."""
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def _init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_record)
        _ORIGINAL_INIT(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
    return seen


def isolate_keystore(monkeypatch, tmp_path) -> None:
    """Point the credential singleton at *tmp_path* and clear provider env vars.

    A saved key takes precedence over the environment, so a test that sets
    ``NVIDIA_API_KEY`` would otherwise pick up a developer's real saved key.
    """
    from velune.providers import keystore as ks

    for name in [*ks.all_provider_env_vars(), ks.PREFER_ENV_VAR]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(ks._manager, "_config_dir", tmp_path)
    monkeypatch.setattr(ks._manager, "_credentials_file", tmp_path / "credentials.json")
    monkeypatch.setattr(ks._manager, "_cache", None)
    monkeypatch.setattr(ks._manager, "_cache_stamp", None)


def refuse_all(request: httpx.Request) -> httpx.Response:
    """A handler for "the network is down": every request fails to connect."""
    raise httpx.ConnectError("connection refused", request=request)


def time_out(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("timed out", request=request)
