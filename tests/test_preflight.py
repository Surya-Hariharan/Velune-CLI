"""Preflight gate tests.

The preflight gate is what stands between a brand-new user and their first
answer, so its two contracts matter:

* One-off ``ask`` (``require_workspace=False``) must succeed in *any* directory
  as long as a model is reachable — no git repo, no index required.
* Codebase ``chat``/``run`` (``require_workspace=True``) must still block until
  the directory is an initialized git workspace.

In both cases the only hard requirement is a reachable model, and a missing one
routes to the right next step (``velune setup`` vs ``velune models scan``).
"""

from __future__ import annotations

from pathlib import Path

from velune.cli.commands.preflight import _no_models_issue, run_preflight_check


class _FakeRegistry:
    def __init__(self, models: list[str]):
        self._models = models

    def list_all(self):
        return self._models


class _FakeContainer:
    """Minimal stand-in for ServiceContainer exposing only .get()."""

    def __init__(self, workspace: Path, models: list[str]):
        self._values = {
            "runtime.workspace": workspace,
            "runtime.model_registry": _FakeRegistry(models),
        }

    def get(self, key: str):
        return self._values[key]


async def test_ask_passes_in_non_repo_when_model_available(tmp_path):
    """A one-off question must work in an empty, non-git directory."""
    container = _FakeContainer(tmp_path, models=["ollama/llama3"])
    ok = await run_preflight_check(container, console=None, require_workspace=False)
    assert ok is True


async def test_ask_blocks_when_no_model(tmp_path):
    """Even a one-off question needs a reachable model."""
    container = _FakeContainer(tmp_path, models=[])
    ok = await run_preflight_check(container, console=None, require_workspace=False)
    assert ok is False


async def test_codebase_command_blocks_in_non_repo(tmp_path):
    """require_workspace=True must reject a directory with no git repo."""
    container = _FakeContainer(tmp_path, models=["ollama/llama3"])
    ok = await run_preflight_check(container, console=None, require_workspace=True)
    assert ok is False


async def test_codebase_command_passes_in_initialized_workspace(tmp_path):
    """A git repo with a .velune index satisfies the workspace contract."""
    (tmp_path / ".git").mkdir()
    (tmp_path / ".velune" / "index").mkdir(parents=True)
    container = _FakeContainer(tmp_path, models=["ollama/llama3"])
    ok = await run_preflight_check(container, console=None, require_workspace=True)
    assert ok is True


def test_no_models_issue_routes_to_setup_without_providers(monkeypatch):
    """With zero providers configured, guidance points at the setup wizard."""
    monkeypatch.setattr(
        "velune.providers.keystore.list_configured_providers",
        lambda *a, **k: [],
    )
    msg = _no_models_issue()
    assert "velune setup" in msg


def test_no_models_issue_routes_to_scan_with_providers(monkeypatch):
    """With providers configured, guidance points at model discovery."""
    monkeypatch.setattr(
        "velune.providers.keystore.list_configured_providers",
        lambda *a, **k: ["groq"],
    )
    msg = _no_models_issue()
    assert "velune models scan" in msg
