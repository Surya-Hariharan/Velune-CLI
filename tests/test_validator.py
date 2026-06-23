"""Unit tests for the post-execution validator.

Covers the three validation branches against a real temp workspace:
expected-file presence, Python syntax compilation, and post-exec test
orchestration. The test-command branch uses a faithful fake sandbox so the
validator's orchestration logic is exercised deterministically without
depending on the host allowlist, PATH, or platform (the real sandbox is
covered separately in the execution-pipeline suite).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from velune.execution.validator import PostExecutionValidator, ValidationResult


class _FakeResult:
    def __init__(self, exit_code: int, stdout: str = "", stderr: str = "") -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

    def to_dict(self) -> dict[str, Any]:
        return {"exit_code": self.exit_code, "stdout": self.stdout, "stderr": self.stderr}


class _FakeSandbox:
    """Mimics the SubprocessSandbox surface the validator depends on."""

    def __init__(self, result: _FakeResult | None = None) -> None:
        self._result = result or _FakeResult(0)
        self.rejections: list[tuple[str, str]] = []
        self.executed: list[Any] = []

    def emit_rejection(self, cmd: str, reason: str) -> None:
        self.rejections.append((cmd, reason))

    def execute(self, spec: Any) -> _FakeResult:
        self.executed.append(spec)
        return self._result


def _validator(workspace: Path, sandbox: _FakeSandbox | None = None) -> PostExecutionValidator:
    return PostExecutionValidator(workspace, sandbox=sandbox)  # type: ignore[arg-type]


class TestExpectedFiles:
    def test_missing_file_is_an_error(self, workspace: Path) -> None:
        result = _validator(workspace, _FakeSandbox()).validate(
            expected_files=[Path("nope.txt")], syntax_check_files=[]
        )
        assert result.success is False
        assert any("not created/found" in e for e in result.errors)
        assert result.details["file_checks"]["nope.txt"] == "missing"

    def test_empty_file_is_an_error(self, workspace: Path) -> None:
        (workspace / "empty.txt").write_text("")
        result = _validator(workspace, _FakeSandbox()).validate(
            expected_files=[Path("empty.txt")], syntax_check_files=[]
        )
        assert result.success is False
        assert any("empty" in e for e in result.errors)
        assert result.details["file_checks"]["empty.txt"] == "empty"

    def test_present_nonempty_file_passes(self, workspace: Path) -> None:
        (workspace / "ok.txt").write_text("content")
        result = _validator(workspace, _FakeSandbox()).validate(
            expected_files=[Path("ok.txt")], syntax_check_files=[]
        )
        assert result.success is True
        assert result.errors == []
        assert result.details["file_checks"]["ok.txt"] == "ok"


class TestSyntaxCheck:
    def test_valid_python_compiles(self, workspace: Path) -> None:
        (workspace / "good.py").write_text("x = 1\n")
        result = _validator(workspace, _FakeSandbox()).validate(
            expected_files=[], syntax_check_files=[Path("good.py")]
        )
        assert result.success is True
        assert result.details["syntax_checks"]["good.py"] == "ok"

    def test_invalid_python_reports_compile_error(self, workspace: Path) -> None:
        (workspace / "bad.py").write_text("def (:\n")
        result = _validator(workspace, _FakeSandbox()).validate(
            expected_files=[], syntax_check_files=[Path("bad.py")]
        )
        assert result.success is False
        assert any("syntax compilation error" in e for e in result.errors)

    def test_non_python_file_is_skipped(self, workspace: Path) -> None:
        (workspace / "app.js").write_text("const x = 1;")
        result = _validator(workspace, _FakeSandbox()).validate(
            expected_files=[], syntax_check_files=[Path("app.js")]
        )
        assert result.success is True
        assert "no built-in parser" in result.details["syntax_checks"]["app.js"]

    def test_missing_syntax_target_is_ignored(self, workspace: Path) -> None:
        result = _validator(workspace, _FakeSandbox()).validate(
            expected_files=[], syntax_check_files=[Path("ghost.py")]
        )
        assert result.success is True
        assert result.details["syntax_checks"] == {}


class TestTestCommandBranch:
    def test_passing_command_keeps_success(self, workspace: Path) -> None:
        sandbox = _FakeSandbox(_FakeResult(exit_code=0, stdout="ok"))
        result = _validator(workspace, sandbox).validate(
            expected_files=[], syntax_check_files=[], test_command="pytest"
        )
        assert result.success is True
        assert len(sandbox.executed) == 1
        assert result.details["test_execution"]["exit_code"] == 0

    def test_failing_command_is_an_error(self, workspace: Path) -> None:
        sandbox = _FakeSandbox(_FakeResult(exit_code=1, stdout="", stderr="boom"))
        result = _validator(workspace, sandbox).validate(
            expected_files=[], syntax_check_files=[], test_command="pytest"
        )
        assert result.success is False
        assert any("exit code 1" in e for e in result.errors)
        assert "boom" in result.errors[0]


class TestValidationResultRepr:
    def test_repr_includes_error_count(self) -> None:
        r = ValidationResult(success=False, errors=["a", "b"], details={})
        assert "errors_count=2" in repr(r)
        assert "success=False" in repr(r)
