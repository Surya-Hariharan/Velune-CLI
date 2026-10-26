"""Suite-wide fixtures.

``ProviderRegistry`` publishes the endpoints it resolves (Ollama host, cloud
base URLs) to :mod:`velune.providers.endpoints` so validation and discovery use
the same ones. That is process-wide state; reset it after every test so one
test's custom base URL cannot redirect another test's requests.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from tests import _sandbox

# ── Persistent-state sandbox ─────────────────────────────────────────────────
# ``velune`` computes a dozen ``Path.home() / ".velune" / ...`` constants when a
# module is first imported, and ``app_data_root()`` falls back to the real
# %LOCALAPPDATA%. A fixture that changes HOME later is too late for the former and
# opt-in for the latter, so earlier runs wrote test debris into the developer's
# real data (hundreds of ``test_*`` workspace dirs, a 311-entry workspace registry,
# the shared telemetry db, the GPU cache). ``pytest_configure`` runs before any
# test module is imported, so everything below resolves inside one throwaway root.
# Set VELUNE_TEST_NO_SANDBOX=1 to opt out (e.g. to debug against real state).
_SANDBOX: Path | None = None
_SANDBOX_BASE: Path | None = None
_SANDBOX_LEFTOVERS: list[tuple[str, str]] = []


def pytest_configure(config):
    global _SANDBOX, _SANDBOX_BASE
    if os.environ.get("VELUNE_TEST_NO_SANDBOX"):
        return
    root, _SANDBOX_BASE = _sandbox.create_sandbox()
    home = root / "home"
    local = home / "AppData" / "Local"
    roaming = home / "AppData" / "Roaming"
    for d in (local, roaming, root / "data", home / ".config", home / ".local" / "share"):
        d.mkdir(parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "HOMEDRIVE": home.drive or "",
        "HOMEPATH": str(home)[len(home.drive) :] if home.drive else str(home),
        "LOCALAPPDATA": str(local),
        "APPDATA": str(roaming),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "VELUNE_DATA_HOME": str(root / "data"),
    }
    os.environ.update({k: v for k, v in env.items() if v})
    _SANDBOX = root
    try:  # never reach the OS credential store from a test
        import keyring
        from keyring.backend import KeyringBackend

        class _MemoryKeyring(KeyringBackend):
            priority = 99

            def __init__(self) -> None:
                super().__init__()
                self._data: dict[tuple[str, str], str] = {}

            def get_password(self, service, username):
                return self._data.get((service, username))

            def set_password(self, service, username, password):
                self._data[(service, username)] = password

            def delete_password(self, service, username):
                self._data.pop((service, username), None)

        keyring.set_keyring(_MemoryKeyring())
    except Exception:  # noqa: S110 - keyring is optional; tests that need it mock it
        pass


def _cleanup_sandbox() -> None:
    """Remove this session's sandbox (idempotent); remember anything that would not go."""
    global _SANDBOX
    root, base = _SANDBOX, _SANDBOX_BASE
    if root is None or base is None:
        return
    try:
        _SANDBOX_LEFTOVERS.extend(_sandbox.remove_sandbox(root, base))
    except RuntimeError as exc:
        _SANDBOX_LEFTOVERS.append((str(root), str(exc)))
    _SANDBOX = None


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    # Runs after a green or a failed run alike, after the last test's fixtures are gone.
    _cleanup_sandbox()
    if _SANDBOX_LEFTOVERS and session.exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter):
    if _SANDBOX_LEFTOVERS:
        terminalreporter.section("test sandbox cleanup FAILED", red=True)
        for path, error in _SANDBOX_LEFTOVERS:
            terminalreporter.write_line(f"{path}: {error}")


def pytest_unconfigure(config):
    # Fallback when sessionfinish never ran (e.g. a crash during collection); a leftover
    # here can only be reported on stderr because the terminal summary is already over.
    _cleanup_sandbox()
    for path, error in _SANDBOX_LEFTOVERS:
        print(f"test sandbox cleanup FAILED: {path}: {error}", file=sys.stderr)


@pytest.fixture(scope="session")
def sandbox_root() -> Path | None:
    return _SANDBOX


# The real-terminal suite needs pyte to read the emulated screen back. CI jobs that
# install only the core test tooling (the lowest-dependency-versions matrix) cannot
# even import it, so skip collecting it there instead of erroring.
collect_ignore_glob = [] if importlib.util.find_spec("pyte") else ["tui/*"]


@pytest.fixture(autouse=True)
def _reset_published_endpoints():
    yield
    from velune.providers import endpoints

    endpoints.configure_ollama_url(None)
    for provider_id in list(endpoints.DEFAULT_BASE_URLS):
        endpoints.configure_base_url(provider_id, None)
