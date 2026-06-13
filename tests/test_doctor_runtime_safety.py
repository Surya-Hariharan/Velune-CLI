"""Doctor runtime-safety check: surfaces PATH-hijack exposure for allowlisted tools.

The check must mirror the real execution guard (``_is_trusted_path``) rather than
re-implement trust heuristics, so these tests stub the shared primitives and
assert the classification the operator would see.
"""

from __future__ import annotations

import pytest

from velune.cli.commands import doctor


@pytest.fixture
def allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the allowlist so the check is deterministic across machines.
    monkeypatch.setattr(
        "velune.execution.command_spec.ALLOWED_EXECUTABLES",
        frozenset({"git", "python"}),
    )


def test_all_trusted_paths_ok(monkeypatch: pytest.MonkeyPatch, allowlist: None) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda exe: rf"C:\Windows\{exe}.exe")
    monkeypatch.setattr("velune.execution.command_spec._is_trusted_path", lambda *a, **k: True)

    result = doctor._check_runtime_safety()
    assert result["status"] == "ok"
    assert "trusted paths" in result["message"]


def test_untrusted_path_warns(monkeypatch: pytest.MonkeyPatch, allowlist: None) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda exe: rf"C:\Users\evil\{exe}.exe")
    monkeypatch.setattr("velune.execution.command_spec._is_trusted_path", lambda *a, **k: False)

    result = doctor._check_runtime_safety()
    assert result["status"] == "warn"
    assert "PATH hijack" in result["message"]
    # The offending tool must be named so the operator can act on it.
    assert "git" in result["message"] or "python" in result["message"]


def test_none_installed_warns(monkeypatch: pytest.MonkeyPatch, allowlist: None) -> None:
    monkeypatch.setattr(doctor.shutil, "which", lambda exe: None)

    result = doctor._check_runtime_safety()
    assert result["status"] == "warn"
    assert "No allowlisted executables" in result["message"]


def test_uses_real_trusted_path_predicate(allowlist: None) -> None:
    # No stubbing of _is_trusted_path: confirms the check wires through to the
    # actual guard and returns a well-formed result without raising.
    result = doctor._check_runtime_safety()
    assert result["name"] == "Runtime Path Safety"
    assert result["status"] in {"ok", "warn"}
    assert isinstance(result["message"], str) and result["message"]
