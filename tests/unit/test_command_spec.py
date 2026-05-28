"""Unit tests for CommandSpec (Batch 13)."""

from pathlib import Path
import pytest
from unittest.mock import patch

from velune.execution.command_spec import CommandSpec
from velune.core.errors.execution import SandboxError


def test_from_string_rejects_shell_operators() -> None:
    """Verify that from_string raises SandboxError if operators like ;, &&, ||, | are present."""
    cwd = Path("/tmp")
    with pytest.raises(SandboxError, match="Shell operator ';'.*found in command argument"):
        CommandSpec.from_string("python main.py; echo bad", cwd)
    with pytest.raises(SandboxError, match="Shell operator '&&'.*found in command argument"):
        CommandSpec.from_string("python main.py && echo bad", cwd)
    with pytest.raises(SandboxError, match="Shell operator '\\|\\|'.*found in command argument"):
        CommandSpec.from_string("python main.py || echo bad", cwd)
    with pytest.raises(SandboxError, match="Shell operator '\\|'.*found in command argument"):
        CommandSpec.from_string("python main.py | grep something", cwd)


def test_from_string_rejects_command_substitution() -> None:
    """Verify that from_string raises SandboxError if command substitution $( or ` is present."""
    cwd = Path("/tmp")
    with pytest.raises(SandboxError, match="Shell operator '\\$\\('.*found in command argument"):
        CommandSpec.from_string("python $(whoami)", cwd)
    with pytest.raises(SandboxError, match="Shell operator '`'.*found in command argument"):
        CommandSpec.from_string("python `whoami`", cwd)


def test_from_string_accepts_valid_python_command() -> None:
    """Verify that from_string correctly parses a valid safe command string."""
    cwd = Path("/tmp")
    spec = CommandSpec.from_string("python main.py --verbose", cwd)
    assert spec.executable == "python"
    assert spec.args == ("main.py", "--verbose")
    assert spec.cwd == cwd


def test_validate_rejects_unlisted_executable() -> None:
    """Verify that validate raises SandboxError for executables not on the allowlist."""
    spec = CommandSpec(executable="rm", args=("-rf", "/"), cwd=Path("/tmp"))
    with pytest.raises(SandboxError, match="is not in the allowed list"):
        spec.validate()


def test_validate_accepts_listed_executable() -> None:
    """Verify that validate succeeds for allowed executables when resolved absolute path exists."""
    spec = CommandSpec(executable="python", args=(), cwd=Path("/tmp"))
    with patch("shutil.which", return_value="/usr/bin/python"):
        with patch("pathlib.Path.resolve", return_value=Path("/usr/bin/python")):
            with patch("pathlib.Path.is_absolute", return_value=True):
                # Should not raise SandboxError
                spec.validate()


def test_from_string_empty_raises_sandbox_error() -> None:
    """Verify that from_string raises SandboxError for an empty/whitespace command string."""
    cwd = Path("/tmp")
    with pytest.raises(SandboxError, match="Empty command"):
        CommandSpec.from_string("", cwd)
    with pytest.raises(SandboxError, match="Empty command"):
        CommandSpec.from_string("   ", cwd)


def test_validate_executable_not_found() -> None:
    """Verify validate raises SandboxError when executable is not found in PATH."""
    spec = CommandSpec(executable="python", args=(), cwd=Path("/tmp"))
    with patch("shutil.which", return_value=None):
        with pytest.raises(SandboxError, match="not found in PATH"):
            spec.validate()


def test_validate_executable_non_absolute() -> None:
    """Verify validate raises SandboxError when executable resolves to a non-absolute path."""
    spec = CommandSpec(executable="python", args=(), cwd=Path("/tmp"))
    with patch("shutil.which", return_value="relative/path"):
        with patch("pathlib.Path.resolve", return_value=Path("relative/path")):
            with patch("pathlib.Path.is_absolute", return_value=False):
                with pytest.raises(SandboxError, match="resolves to non-absolute path"):
                    spec.validate()


def test_to_argv() -> None:
    """Verify to_argv resolves executable path correctly."""
    spec = CommandSpec(executable="python", args=("main.py",), cwd=Path("/tmp"))
    with patch("shutil.which", return_value="/usr/bin/python"):
        assert spec.to_argv() == ["/usr/bin/python", "main.py"]
        
    with patch("shutil.which", return_value=None):
        assert spec.to_argv() == ["python", "main.py"]


def test_from_string_malformed() -> None:
    """Verify from_string raises SandboxError for malformed command strings (e.g. unclosed quotes)."""
    with pytest.raises(SandboxError, match="Malformed command string"):
        CommandSpec.from_string("python \"unclosed quote", Path("/tmp"))


def test_dict_serialization() -> None:
    """Verify to_dict and from_dict roundtrip correctly."""
    spec = CommandSpec(executable="python", args=("main.py",), cwd=Path("/tmp"), timeout=30.0, env_additions={"DEBUG": "1"})
    d = spec.to_dict()
    assert d["executable"] == "python"
    assert d["args"] == ["main.py"]
    assert Path(d["cwd"]) == Path("/tmp")
    assert d["timeout"] == 30.0
    assert d["env_additions"] == {"DEBUG": "1"}
    
    loaded = CommandSpec.from_dict(d)
    assert loaded == spec


def test_malicious_path_rejected(tmp_path, monkeypatch):
    """Executable in untrusted directory must be rejected."""
    # Force platform to not be win32 to test the logic
    monkeypatch.setattr("sys.platform", "linux")
    
    malicious_python = tmp_path / "python"
    malicious_python.write_text("#!/bin/sh\necho evil")
    malicious_python.chmod(0o755)
    
    import shutil
    import os
    original_which = shutil.which
    
    # Mock shutil.which to find the malicious python
    def mock_which(cmd, mode=os.F_OK | os.X_OK, path=None):
        if cmd == "python":
            return str(malicious_python)
        return original_which(cmd, mode, path)
        
    monkeypatch.setattr("shutil.which", mock_which)
    
    original_path = os.environ["PATH"]
    os.environ["PATH"] = f"{tmp_path}:{original_path}"
    
    try:
        spec = CommandSpec.from_string("python --version", cwd=Path.cwd())
        with pytest.raises(SandboxError, match="untrusted path"):
            spec.validate()
    finally:
        os.environ["PATH"] = original_path


def test_system_python_trusted(monkeypatch):
    """System Python must pass trust check."""
    import sys
    import shutil
    if sys.platform == "win32":
        monkeypatch.setattr("shutil.which", lambda cmd: "C:\\Windows\\System32\\echo.exe" if cmd == "echo" else shutil.which(cmd))
    
    spec = CommandSpec.from_string("echo hello", cwd=Path.cwd())
    # Should not raise for system echo
    spec.validate()


def test_validate_uses_runtime_config_when_no_override_provided(monkeypatch) -> None:
    """Verify that validate() loads the allowed_executables from config if not passed."""
    # Create a custom config with only "special_command" allowed
    from velune.kernel.config import VeluneConfig, ExecutionConfig
    custom_config = VeluneConfig(
        execution=ExecutionConfig(
            allowed_executables=["special_command"]
        )
    )
    
    # Patch ConfigLoader.load to return our custom config
    from velune.kernel.config import ConfigLoader
    monkeypatch.setattr(ConfigLoader, "load", lambda self: custom_config)
    
    # Now, CommandSpec for "special_command" should succeed when validate is called without args
    spec_ok = CommandSpec(executable="special_command", args=(), cwd=Path("/tmp"))
    with patch("shutil.which", return_value="/usr/bin/special_command"):
        with patch("pathlib.Path.resolve", return_value=Path("/usr/bin/special_command")):
            with patch("pathlib.Path.is_absolute", return_value=True):
                # Should not raise SandboxError because "special_command" is allowed by the config
                spec_ok.validate()
                
    # CommandSpec for "python" (which is in ALLOWED_EXECUTABLES but not in custom_config) should fail
    spec_fail = CommandSpec(executable="python", args=(), cwd=Path("/tmp"))
    with pytest.raises(SandboxError, match="is not in the allowed list"):
        spec_fail.validate()
