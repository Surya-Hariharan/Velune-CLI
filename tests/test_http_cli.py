"""``/resource configure http`` and workspace discovery of API files (VAIR A3)."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest
from rich.console import Console

from tests.http_fakes import isolate_keystore
from velune.cli.handlers.resources import _configure
from velune.resources.discovery import scan_workspace
from velune.resources.http.auth import load_credential


@pytest.fixture(autouse=True)
def keystore(monkeypatch, tmp_path):
    isolate_keystore(monkeypatch, tmp_path / "keys")


def repl_with(buffer: io.StringIO) -> SimpleNamespace:
    return SimpleNamespace(
        console=Console(file=buffer, force_terminal=False, width=120),
        _resource_manager=SimpleNamespace(discover=None),
    )


def answers(monkeypatch, *values: str) -> list[str]:
    """Feed Prompt.ask a fixed script; returns the prompts it was shown."""
    queue = list(values)
    shown: list[str] = []

    def ask(prompt, *args, **kwargs):
        shown.append(str(prompt))
        return queue.pop(0)

    monkeypatch.setattr("rich.prompt.Prompt.ask", ask)
    return shown


async def test_a_bearer_credential_is_stored_encrypted_and_never_printed(monkeypatch):
    out = io.StringIO()
    answers(monkeypatch, "bearer", "http://localhost:5000", "tok-typed-by-user-123")
    await _configure(repl_with(out), "http aidly-dev-token")
    credential = load_credential("aidly-dev-token")
    assert credential.kind == "bearer" and str(credential.origin) == "http://localhost:5000"
    assert credential.fields["token"] == "tok-typed-by-user-123"
    shown = out.getvalue()
    assert "tok-typed-by-user-123" not in shown and "Saved credential 'aidly-dev-token'" in shown
    assert 'auth = "aidly-dev-token"' in shown


async def test_a_secret_variable_value_needs_no_origin(monkeypatch):
    answers(monkeypatch, "secret", "the-demo-password-1")
    await _configure(repl_with(io.StringIO()), "http demo-password")
    assert load_credential("demo-password").fields["value"] == "the-demo-password-1"


async def test_basic_credentials_collect_both_fields(monkeypatch):
    answers(monkeypatch, "basic", "https://api.example.com", "ada", "pw-typed-123")
    await _configure(repl_with(io.StringIO()), "http basic-one")
    assert load_credential("basic-one").fields["username"] == "ada"


@pytest.mark.parametrize("argument", ["http", "http Bad Ref", "http ../x"])
async def test_a_missing_or_invalid_reference_prints_usage_and_stores_nothing(
    monkeypatch, argument
):
    out = io.StringIO()
    answers(monkeypatch)  # must not prompt
    await _configure(repl_with(out), argument)
    assert "Usage: /resource configure http <reference>" in out.getvalue()


async def test_an_invalid_credential_is_reported_not_saved(monkeypatch):
    out = io.StringIO()
    answers(monkeypatch, "bearer", "not a url", "tok-typed-by-user-123")
    await _configure(repl_with(out), "http broken")
    assert "Not saved" in out.getvalue() and "tok-typed-by-user-123" not in out.getvalue()
    with pytest.raises(Exception, match="not stored"):
        load_credential("broken")


async def test_cancelling_stores_nothing(monkeypatch):
    out = io.StringIO()

    def cancel(prompt, *args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("rich.prompt.Prompt.ask", cancel)
    await _configure(repl_with(out), "http aborted")
    assert "Cancelled" in out.getvalue()


async def test_other_connectors_still_configure_as_before(monkeypatch):
    out = io.StringIO()
    answers(monkeypatch)
    await _configure(repl_with(out), "docker")
    assert "needs no configuration" in out.getvalue()
    out = io.StringIO()
    await _configure(repl_with(out), "")
    assert "http <reference>" in out.getvalue()


def test_openapi_files_and_collections_suggest_the_http_integration(tmp_path):
    assert [h for h in scan_workspace(tmp_path) if h.resource_id == "http"] == []
    (tmp_path / "openapi.yaml").write_text("openapi: 3.0.0\n", encoding="utf-8")
    (tmp_path / ".velune" / "api" / "collections").mkdir(parents=True)
    hints = [h for h in scan_workspace(tmp_path) if h.resource_id == "http"]
    assert {(h.display_name, h.detail) for h in hints} == {
        ("OpenAPI specification", "openapi.yaml"),
        ("API collections", ".velune/api/collections"),
    }
    assert all(not h.suggested for h in hints)  # hints never carry configuration or secrets


async def test_the_workspace_hint_raises_http_in_the_suggestions(tmp_path):
    from tests.http_env import http_connector, http_env
    from velune.resources.base import DiscoveryHint

    env, _ = http_env(tmp_path, http_connector(tmp_path))
    hints = [DiscoveryHint("http", "OpenAPI specification", "openapi.json", "openapi.json")]
    env.source.begin_run("work on the project", hints)
    assert env.source.suggested_ids() == ["http"]
