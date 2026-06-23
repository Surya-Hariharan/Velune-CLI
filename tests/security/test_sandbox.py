"""SubprocessSandbox isolation: rejection paths, env scrubbing, timeout and
process-tree termination."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import psutil
import pytest

from velune.core.errors.execution import SandboxError
from velune.execution.command_spec import CommandSpec
from velune.execution.sandbox import SubprocessSandbox, _kill_process_tree

PYTHON_NAME = Path(sys.executable).name
ALLOWED = [PYTHON_NAME, "git"]


def make_sandbox(workspace: Path, **kwargs) -> SubprocessSandbox:
    return SubprocessSandbox(workspace, allowed_executables=ALLOWED, **kwargs)


def script_spec(workspace: Path, code: str, timeout: float = 30.0) -> CommandSpec:
    """Write *code* to a script in the workspace and build a spec to run it.

    Built directly (not via from_string) so test scripts may contain
    semicolons without tripping the shell-operator parser.
    """
    script = workspace / "script.py"
    script.write_text(code, encoding="utf-8")
    return CommandSpec(
        executable=PYTHON_NAME,
        args=(str(script),),
        cwd=workspace,
        timeout=timeout,
    )


class TestRejection:
    def test_disallowed_executable_rejected(self, workspace: Path) -> None:
        sandbox = make_sandbox(workspace)
        spec = CommandSpec(executable="curl", args=("http://x",), cwd=workspace)
        with pytest.raises(SandboxError):
            sandbox.execute(spec)

    def test_cwd_outside_workspace_rejected(self, workspace: Path, tmp_path: Path) -> None:
        sandbox = make_sandbox(workspace)
        outside = tmp_path / "outside"
        outside.mkdir()
        spec = CommandSpec(executable=PYTHON_NAME, args=("--version",), cwd=outside)
        with pytest.raises(SandboxError, match="outside workspace"):
            sandbox.execute(spec)

    def test_blocked_keyword_in_args_rejected(self, workspace: Path) -> None:
        sandbox = make_sandbox(workspace)
        spec = CommandSpec(executable="git", args=("rm", "-rf", "/"), cwd=workspace)
        with pytest.raises(SandboxError, match="blocked pattern"):
            sandbox.execute(spec)

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo a && echo b",
            "echo a; echo b",
            "cat x | grep y",
            "curl http://evil",
            "wget http://evil",
            "rm -rf /",
        ],
    )
    def test_is_safe_command_blocks_patterns(self, workspace: Path, cmd: str) -> None:
        assert not make_sandbox(workspace)._is_safe_command(cmd)

    def test_is_safe_command_allows_plain_commands(self, workspace: Path) -> None:
        assert make_sandbox(workspace)._is_safe_command("pytest -x tests/")


@pytest.mark.integration
class TestExecution:
    def test_successful_run_captures_output(self, workspace: Path) -> None:
        sandbox = make_sandbox(workspace)
        result = sandbox.execute(script_spec(workspace, "print('velune-ok')"))
        assert result.exit_code == 0
        assert "velune-ok" in result.stdout
        assert result.duration_ms > 0

    def test_nonzero_exit_code_propagated(self, workspace: Path) -> None:
        sandbox = make_sandbox(workspace)
        result = sandbox.execute(script_spec(workspace, "import sys\nsys.exit(3)"))
        assert result.exit_code == 3

    def test_dangerous_env_vars_scrubbed(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LD_PRELOAD", "/tmp/evil.so")
        monkeypatch.setenv("PYTHONSTARTUP", "/tmp/evil.py")
        sandbox = make_sandbox(workspace)
        result = sandbox.execute(
            script_spec(
                workspace,
                "import os\n"
                "print(os.environ.get('LD_PRELOAD', '<unset>'))\n"
                "print(os.environ.get('PYTHONSTARTUP', '<unset>'))\n"
                "print(os.environ.get('PYTHONNOUSERSITE', '<unset>'))",
            )
        )
        lines = result.stdout.splitlines()
        assert lines[0] == "<unset>"
        assert lines[1] == "<unset>"
        assert lines[2] == "1"

    def test_env_additions_passed_through(self, workspace: Path) -> None:
        sandbox = make_sandbox(workspace)
        spec = script_spec(workspace, "import os\nprint(os.environ['VELUNE_TEST_VAR'])")
        spec = CommandSpec(
            executable=spec.executable,
            args=spec.args,
            cwd=spec.cwd,
            timeout=spec.timeout,
            env_additions={"VELUNE_TEST_VAR": "hello"},
        )
        result = sandbox.execute(spec)
        assert "hello" in result.stdout

    def test_high_output_does_not_deadlock(self, workspace: Path) -> None:
        # Regression: a child writing more than the OS pipe buffer (~64 KiB) and
        # exiting immediately used to block on write() because the parent only
        # drained the pipe via communicate() *after* poll() saw the process
        # exit — which never happened. It would then be killed as a false
        # timeout with all output lost. With a generous timeout, a well-behaved
        # fast producer must complete promptly with its output intact.
        start = time.perf_counter()
        sandbox = make_sandbox(workspace)
        result = sandbox.execute(
            script_spec(
                workspace,
                "import sys\nsys.stdout.write('A' * 1_000_000)\nsys.stdout.flush()\n",
                timeout=30.0,
            )
        )
        elapsed = time.perf_counter() - start
        assert result.exit_code == 0
        assert len(result.stdout) == 1_000_000
        assert elapsed < 15, f"fast high-output command took {elapsed:.1f}s (deadlock?)"

    def test_high_output_on_both_streams(self, workspace: Path) -> None:
        # Both pipes must be drained concurrently; filling only stdout's drain
        # while stderr blocks (or vice versa) would still deadlock.
        sandbox = make_sandbox(workspace)
        result = sandbox.execute(
            script_spec(
                workspace,
                "import sys\n"
                "sys.stdout.write('O' * 500_000)\n"
                "sys.stderr.write('E' * 500_000)\n",
                timeout=30.0,
            )
        )
        assert result.exit_code == 0
        assert len(result.stdout) == 500_000
        assert len(result.stderr) == 500_000

    def test_output_capped_at_limit(self, workspace: Path) -> None:
        # A runaway producer must not exhaust parent memory: captured output is
        # bounded and flagged as truncated, but draining continues to EOF so the
        # child never blocks.
        sandbox = make_sandbox(workspace, max_output_bytes=100_000)
        result = sandbox.execute(
            script_spec(
                workspace,
                "import sys\nsys.stdout.write('Z' * 1_000_000)\n",
                timeout=30.0,
            )
        )
        assert result.exit_code == 0
        assert len(result.stdout) < 200_000
        assert "truncated" in result.stdout

    def test_timeout_recovers_partial_output(self, workspace: Path) -> None:
        # On timeout the captured output that was already drained must survive in
        # the error message — the old code lost it because communicate() ran
        # against an already-killed process.
        sandbox = make_sandbox(workspace)
        with pytest.raises(SandboxError, match="timed out") as exc_info:
            sandbox.execute(
                script_spec(
                    workspace,
                    "import sys, time\n"
                    "sys.stdout.write('partial-marker\\n')\n"
                    "sys.stdout.flush()\n"
                    "time.sleep(60)\n",
                    timeout=2.0,
                )
            )
        assert "partial-marker" in str(exc_info.value)


@pytest.mark.integration
@pytest.mark.slow
class TestTermination:
    def test_timeout_kills_process(self, workspace: Path) -> None:
        sandbox = make_sandbox(workspace)
        start = time.perf_counter()
        with pytest.raises(SandboxError, match="timed out"):
            sandbox.execute(script_spec(workspace, "import time\ntime.sleep(60)", timeout=1.5))
        # Must abort near the timeout, not run to completion
        assert time.perf_counter() - start < 30

    def test_timeout_kills_entire_process_tree(self, workspace: Path) -> None:
        # Parent spawns a grandchild, records its PID, then blocks. After the
        # sandbox times out, the grandchild must be dead too — Popen.kill()
        # alone would leave it running unsupervised.
        pid_file = workspace / "child_pid.txt"
        code = (
            "import subprocess, sys, time, pathlib\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
            "time.sleep(120)\n"
        )
        sandbox = make_sandbox(workspace)
        with pytest.raises(SandboxError, match="timed out"):
            sandbox.execute(script_spec(workspace, code, timeout=3.0))

        assert pid_file.exists(), "parent never started"
        grandchild_pid = int(pid_file.read_text())

        # _kill_process_tree waits up to 3s; allow a small grace period
        deadline = time.perf_counter() + 5
        while time.perf_counter() < deadline:
            if not psutil.pid_exists(grandchild_pid):
                break
            try:
                if psutil.Process(grandchild_pid).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"grandchild {grandchild_pid} survived sandbox timeout kill")

    def test_kill_process_tree_handles_dead_pid(self) -> None:
        # Must be a no-op, not an exception, for an already-gone process.
        _kill_process_tree(2**22 + 12345)
