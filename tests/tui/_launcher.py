"""Child-process entry point used by the PTY harness (never imported by tests).

Runs the real ``velune.main.main()`` after redirecting every place the CLI would
touch the *user's* machine: the platformdirs config dir (``credentials.json``,
``master.passphrase``) and the OS keyring (Windows Credential Manager,
macOS Keychain). No application code is modified; the redirect happens before
``velune`` is imported so the ``from platformdirs import user_config_dir``
bindings pick up the patched function.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _isolate() -> None:
    root = Path(os.environ["VELUNE_TUI_SANDBOX"])
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)

    import platformdirs

    def _fake_config_dir(*_a, **_k) -> str:
        return str(config)

    platformdirs.user_config_dir = _fake_config_dir

    import keyring
    from keyring.backend import KeyringBackend

    class _MemoryKeyring(KeyringBackend):
        priority = 99  # type: ignore[assignment]

        def __init__(self) -> None:
            super().__init__()
            self._store: dict[tuple[str, str], str] = {}

        def get_password(self, service, username):
            return self._store.get((service, username))

        def set_password(self, service, username, password):
            self._store[(service, username)] = password

        def delete_password(self, service, username):
            self._store.pop((service, username), None)

    keyring.set_keyring(_MemoryKeyring())

    # Reroute every non-loopback httpx request to the local fake backend, so a
    # "Groq"/"OpenAI" call can never leave the machine. The original host is
    # forwarded in a header so tests can assert on it.
    fake = os.environ.get("VELUNE_TUI_FAKE_HOST")
    if fake:
        import httpx

        loopback = {"127.0.0.1", "localhost", "::1"}

        def _reroute(request: httpx.Request) -> None:
            if request.url.host in loopback:
                return
            request.headers["X-Velune-Orig-Host"] = request.url.host
            request.url = request.url.copy_with(
                scheme="http", host=fake.split(":")[0], port=int(fake.split(":")[1])
            )
            request.headers["host"] = fake

        _orig_async = httpx.AsyncHTTPTransport.handle_async_request
        _orig_sync = httpx.HTTPTransport.handle_request

        async def _async(self, request):
            _reroute(request)
            return await _orig_async(self, request)

        def _sync(self, request):
            _reroute(request)
            return _orig_sync(self, request)

        httpx.AsyncHTTPTransport.handle_async_request = _async  # type: ignore[method-assign]
        httpx.HTTPTransport.handle_request = _sync  # type: ignore[method-assign]


if __name__ == "__main__":
    _isolate()
    from velune.main import main

    sys.argv[0] = "velune"
    main()
