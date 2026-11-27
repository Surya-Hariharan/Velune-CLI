"""Failure-path contracts for `velune ask` and the council.

A bad API key must produce ONE actionable error and a non-zero exit code —
not 20+ raw logger lines, a garbage "answer", and rc=0. These tests pin the
three layers of that contract:

* a definitive 401 rejection is persisted to the keystore so the *next* run
  fails fast at preflight,
* preflight treats models behind a rejected key as unreachable and names the
  fix (`velune provider add <provider>`),
* the ask command maps a failed council run to a clean error + exit 1.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from velune.cli.commands.preflight import run_preflight_check
from velune.core.errors.provider import InferenceError, ProviderAuthenticationError
from velune.swarm.agent import note_provider_failure

GROQ = SimpleNamespace(provider_id="groq")

# ---------------------------------------------------------------------------
# 401 rejection is persisted; other errors are not
# ---------------------------------------------------------------------------


def test_auth_error_marks_key_invalid(monkeypatch):
    recorded = {}

    def _mark(provider_id, *, reason=""):
        recorded["provider"] = provider_id
        recorded["reason"] = reason

    monkeypatch.setattr("velune.providers.keystore.mark_invalid", _mark)
    note_provider_failure(GROQ, ProviderAuthenticationError("401 rejected"))
    assert recorded == {"provider": "groq", "reason": "401 rejected"}


def test_non_auth_error_does_not_touch_key_state(monkeypatch):
    def _mark(provider_id, *, reason=""):
        raise AssertionError("mark_invalid must not be called for non-auth errors")

    monkeypatch.setattr("velune.providers.keystore.mark_invalid", _mark)
    note_provider_failure(GROQ, InferenceError("rate limited"))
    note_provider_failure(GROQ, TimeoutError())


async def test_a_council_agent_rejected_with_401_marks_the_key(monkeypatch, tmp_path):
    from tests.swarm_fakes import ScriptedProvider
    from velune.swarm.agent import AgentEnv
    from velune.swarm.library import SkillLibrary
    from velune.swarm.run import run_swarm

    marked: list[str] = []
    monkeypatch.setattr(
        "velune.providers.keystore.mark_invalid",
        lambda provider_id, *, reason="": marked.append(provider_id),
    )
    provider = ScriptedProvider(lambda request: ProviderAuthenticationError("401 rejected"))
    library = SkillLibrary.load(user_root=Path("does-not-exist"))
    outcome = await run_swarm(
        "explain",
        library,
        AgentEnv(provider=provider, model_id="m"),
        runs_root=tmp_path,
        skill="research",
    )
    assert not outcome.ok and "401 rejected" in outcome.error
    assert marked and set(marked) == {"fake"}


# ---------------------------------------------------------------------------
# Preflight blocks models behind rejected keys
# ---------------------------------------------------------------------------


class _FakeModel:
    def __init__(self, provider_id: str):
        self.provider_id = provider_id


class _FakeRegistry:
    def __init__(self, models):
        self._models = models

    def list_all(self):
        return self._models


class _FakeContainer:
    def __init__(self, workspace: Path, models):
        self._values = {
            "runtime.workspace": workspace,
            "runtime.model_registry": _FakeRegistry(models),
        }

    def get(self, key: str):
        return self._values[key]


async def test_preflight_blocks_when_only_provider_key_rejected(tmp_path, monkeypatch):
    from velune.providers.keystore import KeyState

    monkeypatch.setattr(
        "velune.providers.keystore.verification_state",
        lambda pid: KeyState.INVALID,
    )
    container = _FakeContainer(tmp_path, [_FakeModel("groq")])
    ok = await run_preflight_check(container, console=None, require_workspace=False)
    assert ok is False


async def test_preflight_passes_when_another_provider_still_valid(tmp_path, monkeypatch):
    from velune.providers.keystore import KeyState

    states = {"groq": KeyState.INVALID}
    monkeypatch.setattr(
        "velune.providers.keystore.verification_state",
        lambda pid: states.get(pid, KeyState.VERIFIED),
    )
    container = _FakeContainer(tmp_path, [_FakeModel("groq"), _FakeModel("openai")])
    ok = await run_preflight_check(container, console=None, require_workspace=False)
    assert ok is True


async def test_preflight_blocks_when_only_ollama_models_and_daemon_down(tmp_path, monkeypatch):
    """Manifest-discovered Ollama models can't answer with the daemon down."""
    monkeypatch.setattr("velune.providers.keystore.is_ollama_live", lambda timeout=0.25: False)
    container = _FakeContainer(tmp_path, [_FakeModel("ollama")])
    ok = await run_preflight_check(container, console=None, require_workspace=False)
    assert ok is False


async def test_preflight_passes_with_ollama_models_when_daemon_live(tmp_path, monkeypatch):
    monkeypatch.setattr("velune.providers.keystore.is_ollama_live", lambda timeout=0.25: True)
    container = _FakeContainer(tmp_path, [_FakeModel("ollama")])
    ok = await run_preflight_check(container, console=None, require_workspace=False)
    assert ok is True


# ---------------------------------------------------------------------------
# ask maps a no-answer result to a clean error and exit code 1
# ---------------------------------------------------------------------------


def test_render_council_failure_json_mode(capsys, monkeypatch):
    import json

    from velune.cli.commands.ask import _render_council_failure

    monkeypatch.setattr(
        "velune.providers.keystore.list_invalid_providers",
        lambda: ["groq"],
    )
    ctx = SimpleNamespace(json_mode=True)
    _render_council_failure(ctx, "The council could not produce an answer.")
    payload = json.loads(capsys.readouterr().out)
    assert payload["invalid_providers"] == ["groq"]
    assert payload["error"] == "The council could not produce an answer."


def test_render_council_failure_names_rejected_provider(monkeypatch):
    from velune.cli.commands import ask as ask_mod

    monkeypatch.setattr(
        "velune.providers.keystore.list_invalid_providers",
        lambda: ["groq"],
    )
    import io

    from rich.console import Console

    buffer = io.StringIO()
    monkeypatch.setattr(ask_mod, "console", Console(file=buffer, width=100))
    ctx = SimpleNamespace(json_mode=False)
    ask_mod._render_council_failure(ctx, "No answer.")
    assert "velune provider add groq" in buffer.getvalue()


async def test_ask_exits_nonzero_on_council_failure(monkeypatch, tmp_path, capsys):
    """End-to-end through _ask_with_runtime: a failed council run → typer.Exit(1)."""
    import json

    from velune.cli.commands import ask as ask_mod

    class _Lifecycle:
        async def startup(self):
            pass

        async def shutdown(self):
            pass

    class _Registry:
        async def refresh(self):
            pass

        def list_all(self):
            return [_FakeModel("ollama")]

    async def failed_run(container, task, **kwargs):
        return SimpleNamespace(
            ok=False,
            status="failed",
            content="",
            error="writer failed: ProviderAuthenticationError: 401",
            run_id="r1",
            results=[SimpleNamespace(agent_id="writer", status="error", error="401")],
            plan=SimpleNamespace(
                skill="research", permission="answer-only", output="text", notes=[]
            ),
        )

    class _Container:
        def __init__(self):
            self._values = {
                "runtime.lifecycle": _Lifecycle(),
                "runtime.model_registry": _Registry(),
                "runtime.repository_cognition": SimpleNamespace(index=lambda: None),
                "runtime.workspace": tmp_path,
            }

        def get(self, key):
            return self._values[key]

    monkeypatch.setattr("velune.swarm.setup.run_headless", failed_run)
    monkeypatch.setattr("velune.providers.keystore.list_invalid_providers", lambda: [])
    monkeypatch.setattr("velune.providers.keystore.is_ollama_live", lambda timeout=0.25: True)
    ctx = SimpleNamespace(container=_Container(), json_mode=True, workspace=tmp_path)
    with pytest.raises(typer.Exit) as excinfo:
        await ask_mod._ask_with_runtime(ctx, "hello")
    assert excinfo.value.exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed" and "401" in payload["error"]
    assert payload["agents"] == [{"id": "writer", "status": "error", "error": "401"}]
