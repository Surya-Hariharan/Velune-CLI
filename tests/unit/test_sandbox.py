"""Unit tests for SubprocessSandbox."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from velune.core.errors.execution import SandboxError
from velune.execution.command_spec import CommandSpec
from velune.execution.sandbox import SubprocessSandbox


@pytest.fixture
def workspace_path(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def sandbox(workspace_path: Path) -> SubprocessSandbox:
    return SubprocessSandbox(
        workspace_path=workspace_path,
        max_memory_mb=50.0,
        max_cpu_percent=80.0,
        allowed_executables=["python", "echo"],
    )


def test_sandbox_initialization(workspace_path: Path) -> None:
    sandbox = SubprocessSandbox(workspace_path)
    assert sandbox.workspace_path == workspace_path.resolve()
    assert sandbox.max_memory_mb == 1024.0
    assert sandbox.max_cpu_percent == 90.0


def test_is_safe_command(sandbox: SubprocessSandbox) -> None:
    # Safe commands
    assert sandbox._is_safe_command("python script.py") is True
    assert sandbox._is_safe_command("echo hello") is True

    # Blocked keywords
    assert sandbox._is_safe_command("rm -rf /") is False
    assert sandbox._is_safe_command("shutdown now") is False

    # Blocked utilities
    assert sandbox._is_safe_command("curl http://example.com") is False
    assert sandbox._is_safe_command("wget http://example.com") is False
    assert sandbox._is_safe_command("base64 -d") is False

    # Shell chaining
    assert sandbox._is_safe_command("echo 1 && echo 2") is False
    assert sandbox._is_safe_command("echo 1 ; echo 2") is False
    assert sandbox._is_safe_command("echo 1 | grep 2") is False


def test_is_safe_path(sandbox: SubprocessSandbox) -> None:
    safe_path = sandbox.workspace_path / "src" / "main.py"
    unsafe_path = sandbox.workspace_path.parent / "outside.py"

    assert sandbox._is_safe_path(safe_path) is True
    assert sandbox._is_safe_path(unsafe_path) is False


@pytest.mark.asyncio
async def test_emit_rejection(sandbox: SubprocessSandbox) -> None:
    mock_bus = MagicMock()
    mock_bus.emit = AsyncMock()
    sandbox.bus = mock_bus

    sandbox.emit_rejection("curl bad", "SSRF attempt")
    await asyncio.sleep(0.01)  # Allow event loop to schedule task if running
    # Note: loop might not be running in this test thread, but emit_rejection is safe


def test_execute_blocked_command(sandbox: SubprocessSandbox) -> None:
    spec = CommandSpec(
        executable="python",
        args=["-c", "import os; os.system('curl bad')"],
        cwd=sandbox.workspace_path,
    )
    with pytest.raises(SandboxError, match="blocked pattern"):
        sandbox.execute(spec)


def test_execute_outside_workspace(sandbox: SubprocessSandbox) -> None:
    spec = CommandSpec(
        executable="python",
        args=["script.py"],
        cwd=sandbox.workspace_path.parent,
    )
    with pytest.raises(SandboxError, match="outside workspace"):
        sandbox.execute(spec)


def test_execute_unallowed_executable(sandbox: SubprocessSandbox) -> None:
    spec = CommandSpec(
        executable="unallowed_bin",
        args=[],
        cwd=sandbox.workspace_path,
    )
    with pytest.raises(SandboxError, match="not in the allowed list"):
        sandbox.execute(spec)


@patch("subprocess.Popen")
@patch("psutil.Process")
def test_execute_environment_cleaning(
    mock_psutil_proc: MagicMock,
    mock_popen: MagicMock,
    sandbox: SubprocessSandbox,
) -> None:
    # Configure mock Popen
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.poll.return_value = 0
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = ("stdout", "stderr")
    mock_popen.return_value = mock_proc

    # Configure mock psutil
    mock_ps_proc = MagicMock()
    mock_ps_proc.memory_info.return_value = MagicMock(rss=10 * 1024 * 1024)
    mock_ps_proc.cpu_percent.return_value = 5.0
    mock_psutil_proc.return_value = mock_ps_proc

    spec = CommandSpec(
        executable="python",
        args=["-c", "print(1)"],
        cwd=sandbox.workspace_path,
        env_additions={"LD_PRELOAD": "hijack.so", "CUSTOM_VAR": "value"},
    )

    result = sandbox.execute(spec)
    assert result.exit_code == 0
    assert result.stdout == "stdout"

    # Assert subprocess.Popen environment did NOT contain LD_PRELOAD
    called_env = mock_popen.call_args[1]["env"]
    assert "LD_PRELOAD" not in called_env
    assert called_env["CUSTOM_VAR"] == "value"
    assert called_env["PYTHONNOUSERSITE"] == "1"


@patch("subprocess.Popen")
@patch("psutil.Process")
def test_execute_timeout(
    mock_psutil_proc: MagicMock,
    mock_popen: MagicMock,
    sandbox: SubprocessSandbox,
) -> None:
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    # Keep process active
    mock_proc.poll.side_effect = [None, None, None, 0]
    mock_proc.communicate.return_value = ("stdout", "stderr")
    mock_popen.return_value = mock_proc

    mock_ps_proc = MagicMock()
    mock_ps_proc.memory_info.return_value = MagicMock(rss=10 * 1024 * 1024)
    mock_ps_proc.cpu_percent.return_value = 5.0
    mock_psutil_proc.return_value = mock_ps_proc

    spec = CommandSpec(
        executable="python",
        args=["-c", "import time\ntime.sleep(10)"],
        cwd=sandbox.workspace_path,
        timeout=0.01,  # Short timeout
    )

    with pytest.raises(SandboxError, match="timed out"):
        sandbox.execute(spec)
    mock_proc.kill.assert_called()


def test_sandbox_for_workspace(workspace_path: Path) -> None:
    sb = SubprocessSandbox.for_workspace(workspace_path)
    assert sb.workspace_path == workspace_path.resolve()


def test_sandbox_result_to_dict() -> None:
    from velune.execution.sandbox import SandboxResult
    res = SandboxResult(
        exit_code=0,
        stdout="out",
        stderr="err",
        duration_ms=120.0,
        peak_memory_mb=12.5,
        peak_cpu_pct=45.0,
    )
    d = res.to_dict()
    assert d["exit_code"] == 0
    assert d["stdout"] == "out"
    assert d["stderr"] == "err"
    assert d["duration_ms"] == 120.0
    assert d["peak_memory_mb"] == 12.5
    assert d["peak_cpu_pct"] == 45.0


@patch("subprocess.Popen")
@patch("psutil.Process")
def test_execute_memory_threshold_exceeded(
    mock_psutil_proc: MagicMock,
    mock_popen: MagicMock,
    sandbox: SubprocessSandbox,
) -> None:
    # Set low memory limit
    sandbox.max_memory_mb = 5.0

    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.poll.side_effect = [None, None]
    mock_popen.return_value = mock_proc

    mock_ps_proc = MagicMock()
    mock_ps_proc.memory_info.return_value = MagicMock(rss=10 * 1024 * 1024)  # 10MB > 5MB
    mock_ps_proc.cpu_percent.return_value = 5.0
    mock_psutil_proc.return_value = mock_ps_proc

    spec = CommandSpec(
        executable="python",
        args=["-c", "print(1)"],
        cwd=sandbox.workspace_path,
    )

    with pytest.raises(SandboxError, match="Memory threshold exceeded"):
        sandbox.execute(spec)

    mock_proc.terminate.assert_called_once()


@patch("subprocess.Popen")
def test_execute_spawn_failure(
    mock_popen: MagicMock,
    sandbox: SubprocessSandbox,
) -> None:
    mock_popen.side_effect = Exception("OS Error spawning process")

    spec = CommandSpec(
        executable="python",
        args=["-c", "print(1)"],
        cwd=sandbox.workspace_path,
    )

    with pytest.raises(SandboxError, match="Failed to spawn subprocess"):
        sandbox.execute(spec)


@pytest.mark.asyncio
async def test_emit_rejection_error_handling(sandbox: SubprocessSandbox) -> None:
    # Cause emitting to raise an exception
    mock_bus = MagicMock()
    mock_bus.emit.side_effect = Exception("Bus connection lost")
    sandbox.bus = mock_bus

    # Verify calling emit_rejection handles the error and does not raise
    sandbox.emit_rejection("python bad", "some error")


def test_sandbox_default_executables_fallback(workspace_path: Path) -> None:
    # Tests that when config parsing fails, we fallback to DEFAULT_EXECUTABLES
    sb = SubprocessSandbox(workspace_path=workspace_path)
    assert len(sb.allowed_executables) > 0

