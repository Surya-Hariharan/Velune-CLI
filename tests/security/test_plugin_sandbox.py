"""Plugin sandbox isolation tests.

Verifies that plugins executing via PluginSandbox cannot access parent
environment variables, cannot exceed wall-clock timeouts, and that
well-behaved plugins round-trip results correctly.
"""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from velune.plugins.sandbox import PluginSandbox, PluginSandboxError


def _write_plugin(tmp_path: Path, code: str, hook_name: str = "on_pre_execute") -> Path:
    """Write a minimal plugin module and return its directory."""
    plugin_dir = tmp_path / "test_plugin"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(textwrap.dedent(code), encoding="utf-8")
    return plugin_dir


def test_credential_not_inherited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plugin subprocess must not see parent env vars like ANTHROPIC_API_KEY."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-top-secret")

    plugin_dir = _write_plugin(
        tmp_path,
        """
        import os, json
        class Plugin:
            def on_pre_execute(self, **kwargs):
                return os.environ.get("ANTHROPIC_API_KEY", "NOT_FOUND")
        """,
    )

    sandbox = PluginSandbox(timeout=15)
    result = sandbox.run_hook(
        plugin_dir=plugin_dir,
        entry_point="plugin.py",
        class_name="Plugin",
        hook_name="on_pre_execute",
    )
    assert result == "NOT_FOUND", (
        f"Plugin saw ANTHROPIC_API_KEY in sandbox env — got: {result!r}"
    )


def test_timeout_kills_plugin(tmp_path: Path) -> None:
    """A plugin that loops forever must be killed after the timeout."""
    plugin_dir = _write_plugin(
        tmp_path,
        """
        import time
        class Plugin:
            def on_pre_execute(self, **kwargs):
                while True:
                    time.sleep(1)
        """,
    )

    sandbox = PluginSandbox(timeout=3)
    with pytest.raises(PluginSandboxError, match="timed out"):
        sandbox.run_hook(
            plugin_dir=plugin_dir,
            entry_point="plugin.py",
            class_name="Plugin",
            hook_name="on_pre_execute",
        )


def test_well_behaved_plugin_round_trips_result(tmp_path: Path) -> None:
    """A cooperative plugin's return value must be returned correctly."""
    plugin_dir = _write_plugin(
        tmp_path,
        """
        class Plugin:
            def on_pre_execute(self, task="", **kwargs):
                return {"echo": task, "status": "ok"}
        """,
    )

    sandbox = PluginSandbox(timeout=15)
    result = sandbox.run_hook(
        plugin_dir=plugin_dir,
        entry_point="plugin.py",
        class_name="Plugin",
        hook_name="on_pre_execute",
        payload={"task": "hello"},
    )
    assert result == {"echo": "hello", "status": "ok"}


def test_plugin_exception_raises_sandbox_error(tmp_path: Path) -> None:
    """A plugin that raises an exception must surface as PluginSandboxError."""
    plugin_dir = _write_plugin(
        tmp_path,
        """
        class Plugin:
            def on_pre_execute(self, **kwargs):
                raise RuntimeError("intentional plugin crash")
        """,
    )

    sandbox = PluginSandbox(timeout=15)
    with pytest.raises(PluginSandboxError, match="intentional plugin crash"):
        sandbox.run_hook(
            plugin_dir=plugin_dir,
            entry_point="plugin.py",
            class_name="Plugin",
            hook_name="on_pre_execute",
        )


def test_missing_hook_returns_none(tmp_path: Path) -> None:
    """If the plugin class doesn't define the requested hook, the result is None."""
    plugin_dir = _write_plugin(
        tmp_path,
        """
        class Plugin:
            pass
        """,
    )

    sandbox = PluginSandbox(timeout=15)
    result = sandbox.run_hook(
        plugin_dir=plugin_dir,
        entry_point="plugin.py",
        class_name="Plugin",
        hook_name="on_pre_execute",
    )
    assert result is None


def test_async_hook_executes_correctly(tmp_path: Path) -> None:
    """Async hooks must be awaited inside the subprocess and the result returned."""
    plugin_dir = _write_plugin(
        tmp_path,
        """
        import asyncio
        class Plugin:
            async def on_pre_execute(self, value=0, **kwargs):
                await asyncio.sleep(0)
                return value * 2
        """,
    )

    sandbox = PluginSandbox(timeout=15)
    result = sandbox.run_hook(
        plugin_dir=plugin_dir,
        entry_point="plugin.py",
        class_name="Plugin",
        hook_name="on_pre_execute",
        payload={"value": 21},
    )
    assert result == 42
