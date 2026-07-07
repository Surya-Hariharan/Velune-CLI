"""Provider pillar edge cases: first-provider-as-default + CLI-native guidance.

Two friction fixes:

* Adding your *first* provider should adopt it as the default automatically
  (like git/gh with the first remote/account), without a separate
  ``velune provider default`` step — but it must never override an existing
  choice.
* The "next steps" surfaced after ``velune provider add`` / ``velune models
  scan`` are printed to a shell, so they must be real ``velune ...`` commands,
  not REPL slash-commands like ``/models scan``.
"""

from __future__ import annotations

import toml

from velune.cli import guidance
from velune.cli.commands import providers as prov


def test_first_provider_becomes_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # No velune.toml yet → no default configured.
    assert prov._get_default_provider() is None

    assert prov._maybe_set_first_default("openai") is True

    data = toml.load(tmp_path / "velune.toml")
    assert data["providers"]["default_provider"] == "openai"


def test_existing_default_is_not_overridden(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "velune.toml").write_text(
        toml.dumps({"providers": {"default_provider": "anthropic"}}), encoding="utf-8"
    )

    # A second provider must not steal the default.
    assert prov._maybe_set_first_default("groq") is False
    data = toml.load(tmp_path / "velune.toml")
    assert data["providers"]["default_provider"] == "anthropic"


def test_provider_added_guidance_is_cli_native():
    steps = guidance.steps_for("provider_added", model="gpt-4o")
    commands = [cmd for _, cmd, _ in steps]
    # No REPL slash-commands leak into shell-facing guidance.
    assert all(not cmd.startswith("/") for cmd in commands)
    assert "velune models scan" in commands
    assert "velune models use gpt-4o" in commands


def test_models_scanned_guidance_is_cli_native():
    steps = guidance.steps_for("models_scanned", model="llama3.2:3b")
    commands = [cmd for _, cmd, _ in steps]
    assert all(not cmd.startswith("/") for cmd in commands)
    assert "velune models use llama3.2:3b" in commands
