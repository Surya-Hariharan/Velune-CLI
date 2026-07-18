"""Failure-path contracts for `velune ask` and the council pipeline.

A bad API key must produce ONE actionable error and a non-zero exit code —
not 20+ raw logger lines, a garbage "answer", and rc=0. These tests pin the
four layers of that contract:

* agents that fail return sentinel strings; those must never survive the
  coder candidate vote (`_diverge_candidates`),
* a definitive 401 rejection is persisted to the keystore so the *next* run
  fails fast at preflight,
* preflight treats models behind a rejected key as unreachable and names the
  fix (`velune login <provider>`),
* the ask command maps a no-answer council result to a clean error + exit 1.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from velune.cli.commands.preflight import run_preflight_check
from velune.cognition.council.base import BaseCouncilAgent
from velune.core.errors.provider import InferenceError, ProviderAuthenticationError

# ---------------------------------------------------------------------------
# Agent failure sentinels must not become coder candidates
# ---------------------------------------------------------------------------


class _FakeJobResult:
    def __init__(self, value, ok=True):
        self.value = value
        self.ok = ok


class _FakeScheduler:
    def __init__(self, results):
        self._results = results
        self.last_mode = "sequential"

    async def run(self, jobs, timeout):
        return self._results


async def test_diverge_drops_failure_sentinel_strings():
    """'Deliberation failure…' strings are errors, not candidate solutions."""
    from velune.cognition.orchestrator import CouncilOrchestrator

    orch = CouncilOrchestrator.__new__(CouncilOrchestrator)
    orch.scheduler = _FakeScheduler(
        [
            _FakeJobResult("Deliberation failure inside agent coder: 401 Unauthorized"),
            _FakeJobResult("[Agent coder timed out — using empty response]"),
            _FakeJobResult("def real_code(): ..."),
        ]
    )
    coder = SimpleNamespace(
        model=SimpleNamespace(provider_id="groq"),
        write_code=None,
    )
    candidates = await orch._diverge_candidates(
        coder,
        prompt="p",
        current_code="",
        plan_context="",
        style_profile=None,
        format_instructions="",
        n_samples=3,
        timeout=1.0,
    )
    assert candidates == ["def real_code(): ..."]


def test_all_failed_result_carries_honest_flag():
    """Total failure reports ALL_AGENTS_FAILED, not a fake timeout, and a str tier."""
    from velune.cognition.orchestrator import CouncilOrchestrator

    orch = CouncilOrchestrator.__new__(CouncilOrchestrator)
    res = orch._build_timeout_result(
        "prompt",
        "instant",
        flag="ALL_AGENTS_FAILED",
        summary="The council could not produce an answer.",
        critical_issue="Every Coder attempt failed.",
    )
    assert res["is_timeout"] is True
    assert res["tier"] == "instant"  # regression: was a float start_time
    assert res["arbitration"]["flags"] == ["ALL_AGENTS_FAILED"]
    assert "could not produce an answer" in res["final_summary"]


# ---------------------------------------------------------------------------
# 401 rejection is persisted; other errors are not
# ---------------------------------------------------------------------------


def test_auth_error_marks_key_invalid(monkeypatch):
    recorded = {}

    def _mark(provider_id, *, reason=""):
        recorded["provider"] = provider_id
        recorded["reason"] = reason

    monkeypatch.setattr("velune.providers.keystore.mark_invalid", _mark)
    BaseCouncilAgent._note_provider_failure("groq", ProviderAuthenticationError("401 rejected"))
    assert recorded == {"provider": "groq", "reason": "401 rejected"}


def test_non_auth_error_does_not_touch_key_state(monkeypatch):
    def _mark(provider_id, *, reason=""):
        raise AssertionError("mark_invalid must not be called for non-auth errors")

    monkeypatch.setattr("velune.providers.keystore.mark_invalid", _mark)
    BaseCouncilAgent._note_provider_failure("groq", InferenceError("rate limited"))
    BaseCouncilAgent._note_provider_failure("groq", TimeoutError())


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
    _render_council_failure(ctx, "The council could not produce an answer.", ["ALL_AGENTS_FAILED"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["flags"] == ["ALL_AGENTS_FAILED"]
    assert payload["invalid_providers"] == ["groq"]
    assert "error" in payload


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
    ask_mod._render_council_failure(ctx, "No answer.", ["ALL_AGENTS_FAILED"])
    assert "velune login groq" in buffer.getvalue()


async def test_ask_exits_nonzero_on_council_failure(monkeypatch, tmp_path):
    """End-to-end through _ask_with_runtime: is_timeout result → typer.Exit(1)."""
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

    class _Orchestrator:
        mapper = SimpleNamespace(map_roles=lambda self=None: {})

        async def execute_task(self, prompt, repo_context, council_tier=None):
            return {
                "tier": "instant",
                "arbitration": {"flags": ["ALL_AGENTS_FAILED"]},
                "final_summary": "The council could not produce an answer.",
                "reviewer_report": None,
                "challenger_report": None,
                "is_timeout": True,
            }

    class _Container:
        def __init__(self):
            self._values = {
                "runtime.lifecycle": _Lifecycle(),
                "runtime.model_registry": _Registry(),
                "runtime.council_orchestrator": _Orchestrator(),
                "runtime.repository_cognition": SimpleNamespace(index=lambda: None),
                "runtime.workspace": tmp_path,
            }

        def get(self, key):
            return self._values[key]

    monkeypatch.setattr(
        "velune.providers.keystore.list_invalid_providers",
        lambda: [],
    )
    ctx = SimpleNamespace(container=_Container(), json_mode=True, workspace=tmp_path)
    with pytest.raises(typer.Exit) as excinfo:
        await ask_mod._ask_with_runtime(ctx, "hello")
    assert excinfo.value.exit_code == 1
