"""Docker connection selection, TLS rules and client lifecycle (VAIR A4)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.docker_fakes import APIError, ConnectionError, FakeDaemon, NotFound, make_engine
from velune.resources.base import ErrorCode
from velune.resources.docker.engine import (
    DockerEngine,
    Endpoint,
    EngineError,
    TlsOptions,
    candidate_endpoints,
    classify_endpoint,
    map_error,
    read_context_endpoint,
)

HOME = Path("/home/user")
TLS = TlsOptions("/c/ca.pem", "/c/cert.pem", "/c/key.pem")


def cfg(**kw):
    return SimpleNamespace(
        host=kw.get("host", ""),
        tls_ca=kw.get("ca", ""),
        tls_cert=kw.get("cert", ""),
        tls_key=kw.get("key", ""),
        timeout=5,
    )


def urls(candidates):
    return [c.base_url if isinstance(c, Endpoint) else c for c in candidates]


# ── Selecting an endpoint per platform ──────────────────────────────────────


def test_windows_uses_the_docker_desktop_named_pipes():
    found = candidate_endpoints(None, {}, "win32", HOME)
    assert urls(found) == [
        "npipe:////./pipe/docker_engine",
        "npipe:////./pipe/dockerDesktopLinuxEngine",
    ]


def test_linux_tries_the_system_socket_then_rootless_then_desktop():
    found = candidate_endpoints(None, {"XDG_RUNTIME_DIR": "/run/user/1000"}, "linux", HOME)
    assert urls(found) == [
        "unix:///var/run/docker.sock",
        "unix:///run/user/1000/docker.sock",
        f"unix://{HOME}/.docker/desktop/docker.sock",
    ]


def test_macos_adds_the_per_user_docker_desktop_socket():
    found = urls(candidate_endpoints(None, {}, "darwin", HOME))
    assert found[0] == "unix:///var/run/docker.sock"
    assert f"unix://{HOME}/.docker/run/docker.sock" in found


def test_docker_host_beats_the_defaults_and_is_never_silently_replaced():
    found = candidate_endpoints(None, {"DOCKER_HOST": "unix:///custom.sock"}, "linux", HOME)
    assert urls(found) == ["unix:///custom.sock"]
    bad = candidate_endpoints(None, {"DOCKER_HOST": "gopher://x"}, "linux", HOME)
    assert len(bad) == 1 and isinstance(bad[0], EngineError)  # reported, not replaced by a default


def test_explicit_configuration_wins_over_the_environment():
    found = candidate_endpoints(
        cfg(host="unix:///cfg.sock"), {"DOCKER_HOST": "unix:///env.sock"}, "linux", HOME
    )
    assert urls(found) == ["unix:///cfg.sock"] and found[0].source == "config"


def test_configuration_from_an_untrusted_workspace_cannot_redirect_docker():
    found = candidate_endpoints(
        cfg(host="ssh://evil@attacker.example"),
        {"DOCKER_HOST": "unix:///env.sock"},
        "linux",
        HOME,
        trust_config=False,
    )
    assert urls(found) == ["unix:///env.sock"]  # the user's own environment still applies


def test_the_active_docker_context_is_read(tmp_path):
    name = "remote-dev"
    meta = tmp_path / ".docker" / "contexts" / "meta" / hashlib.sha256(name.encode()).hexdigest()
    meta.mkdir(parents=True)
    (meta / "meta.json").write_text(
        json.dumps({"Endpoints": {"docker": {"Host": "unix:///ctx.sock"}}})
    )
    (tmp_path / ".docker" / "config.json").write_text(json.dumps({"currentContext": name}))
    assert read_context_endpoint(tmp_path, {}) == "unix:///ctx.sock"
    assert urls(candidate_endpoints(None, {}, "linux", tmp_path)) == ["unix:///ctx.sock"]
    assert read_context_endpoint(tmp_path, {"DOCKER_CONTEXT": "default"}) is None


@pytest.mark.parametrize(
    "content", ["{nope", json.dumps({"currentContext": "missing"}), json.dumps([])]
)
def test_a_broken_context_falls_back_to_the_platform_default(tmp_path, content):
    (tmp_path / ".docker").mkdir()
    (tmp_path / ".docker" / "config.json").write_text(content)
    assert read_context_endpoint(tmp_path, {}) is None
    assert (
        urls(candidate_endpoints(None, {}, "linux", tmp_path))[0] == "unix:///var/run/docker.sock"
    )


# ── Remote endpoints: never unauthenticated, never weakened ─────────────────


@pytest.mark.parametrize(
    "url", ["tcp://10.0.0.5:2375", "http://host:2375", "tcp://127.0.0.1:2375", "https://host:2376"]
)
def test_a_network_socket_without_tls_is_refused(url):
    with pytest.raises(EngineError) as caught:
        classify_endpoint(url, "config", tls=None)
    assert (
        caught.value.code is ErrorCode.AUTHENTICATION_FAILED
        and "root on that host" in caught.value.message
    )
    assert "tls" in caught.value.hint.lower()


def test_a_network_socket_with_client_certificates_is_allowed_and_marked_remote():
    endpoint = classify_endpoint("tcp://build.example:2376", "config", tls=TLS)
    assert (
        endpoint.remote
        and endpoint.tls == TLS
        and endpoint.base_url == "https://build.example:2376"
    )
    assert "key.pem" not in endpoint.describe() and endpoint.describe() == "https://build.example"


def test_ssh_endpoints_use_the_system_ssh_client_and_are_remote():
    endpoint = classify_endpoint("ssh://dev@host", "config", tls=None)
    assert endpoint.ssh and endpoint.remote


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://x",
        "file:///var/run/docker.sock",
        "npipe://remote/pipe/x",
        "unix:relative",
        "tcp://",
    ],
)
def test_unsupported_or_malformed_endpoints_are_refused(url):
    with pytest.raises(EngineError):
        classify_endpoint(url, "config", tls=TLS)


def test_tls_comes_from_configuration_paths_or_the_standard_environment():
    found = candidate_endpoints(
        cfg(host="tcp://h:2376", ca="/a", cert="/b", key="/c"), {}, "linux", HOME
    )
    assert found[0].tls == TlsOptions("/a", "/b", "/c")
    env = {"DOCKER_HOST": "tcp://h:2376", "DOCKER_TLS_VERIFY": "1", "DOCKER_CERT_PATH": "/certs"}
    tls = candidate_endpoints(None, env, "linux", HOME)[0].tls
    assert tls is not None and tls.ca_cert.replace("\\", "/") == "/certs/ca.pem"


def test_the_sdk_client_is_built_with_verification_on(monkeypatch):
    docker = pytest.importorskip("docker")
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(docker, "DockerClient", Client)
    monkeypatch.setattr(docker.tls, "TLSConfig", lambda **kw: SimpleNamespace(**kw))
    from velune.resources.docker.engine import sdk_client_factory

    sdk_client_factory(classify_endpoint("tcp://h:2376", "config", tls=TLS), 7)
    assert (
        seen["tls"].verify is True
        and seen["tls"].ca_cert == "/c/ca.pem"
        and seen["version"] == "auto"
    )
    assert seen["timeout"] == 7


# ── Connection lifecycle ────────────────────────────────────────────────────


def test_connect_verifies_reachability_and_reports_versions():
    daemon = FakeDaemon()
    engine = make_engine(daemon)
    summary = engine.connect_sync()
    assert summary["version"] == "27.0.1" and summary["api_version"] == "1.45"
    assert engine.connected and not engine.remote


def test_the_client_is_reused_not_reopened():
    daemon = FakeDaemon()
    engine = make_engine(daemon)
    for _ in range(5):
        engine.call_sync(lambda c: c.ping(), "ping")
    assert daemon.clients_opened == 1


def test_an_unreachable_daemon_is_a_clear_error_not_an_installed_cli_guess():
    daemon = FakeDaemon()
    daemon.down = True
    engine = make_engine(daemon)
    with pytest.raises(EngineError) as caught:
        engine.connect_sync()
    assert (
        caught.value.code is ErrorCode.UNAVAILABLE
        and "not running or not reachable" in caught.value.message
    )
    assert "Start Docker" in caught.value.hint and not engine.connected
    assert daemon.clients_closed == daemon.clients_opened  # no half-open client is left behind


def test_an_engine_that_is_too_old_is_refused_with_a_version_message():
    daemon = FakeDaemon()
    daemon.api_version = "1.24"
    with pytest.raises(EngineError) as caught:
        make_engine(daemon).connect_sync()
    assert (
        "older than the supported 1.41" in caught.value.message
        and "Update Docker" in caught.value.hint
    )


def test_a_daemon_restart_reconnects_for_reads_and_closes_the_stale_client():
    daemon = FakeDaemon()
    daemon.add_container("web")
    engine = make_engine(daemon)
    assert (
        len(engine.call_sync(lambda c: c.containers.list(all=True), "list", idempotent=True)) == 1
    )
    attempts = {"n": 0}

    def flaky(client):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("connection reset by the restarting daemon")
        return client.containers.list(all=True)

    assert len(engine.call_sync(flaky, "list", idempotent=True)) == 1
    assert daemon.clients_opened == 2 and daemon.clients_closed == 1


def test_a_change_is_never_retried_after_the_connection_drops():
    daemon = FakeDaemon()
    engine = make_engine(daemon)
    calls = {"n": 0}

    def change(client):
        calls["n"] += 1
        raise ConnectionError("lost")

    with pytest.raises(EngineError) as caught:
        engine.call_sync(change, "starting a container")
    assert (
        calls["n"] == 1 and caught.value.code is ErrorCode.UNAVAILABLE
    )  # not repeated: it might have run
    assert not engine.connected  # but the dead client was dropped


def test_close_releases_the_client_and_is_safe_twice():
    daemon = FakeDaemon()
    engine = make_engine(daemon)
    engine.connect_sync()
    engine.close()
    engine.close()
    assert daemon.clients_closed == 1 and not engine.connected


async def test_ping_reports_liveness_without_raising():
    daemon = FakeDaemon()
    engine = make_engine(daemon)
    assert await engine.ping() is True
    daemon.down = True
    assert await engine.ping() is False and not engine.connected


def test_missing_sdk_says_how_to_install_it(monkeypatch):
    import builtins

    real = builtins.__import__

    def deny(name, *a, **k):
        if name == "docker":
            raise ImportError("no docker")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", deny)
    engine = DockerEngine(environ={}, platform="linux", home=HOME)
    with pytest.raises(EngineError) as caught:
        engine.connect_sync()
    assert "velune-cli[docker]" in caught.value.hint


# ── Error normalization ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (NotFound("No such container: x"), ErrorCode.NOT_FOUND),
        (APIError("name in use", 409), ErrorCode.CONFLICT),
        (APIError("forbidden", 403), ErrorCode.AUTHORIZATION_DENIED),
        (APIError("unauthorized", 401), ErrorCode.AUTHENTICATION_FAILED),
        (APIError("boom", 500), ErrorCode.ADAPTER_FAILURE),
        (ConnectionError("down"), ErrorCode.UNAVAILABLE),
        (FileNotFoundError("npipe"), ErrorCode.UNAVAILABLE),
        (TimeoutError(), ErrorCode.TIMEOUT),
        (ValueError("odd"), ErrorCode.ADAPTER_FAILURE),
    ],
)
def test_sdk_and_transport_errors_are_normalized(exc, code):
    assert map_error(exc).code is code


def test_error_text_is_redacted_and_bounded():
    error = map_error(APIError("pull failed for password=hunter2hunter2 " + "x" * 2000, 500))
    assert "hunter2hunter2" not in error.message and len(error.message) < 400
