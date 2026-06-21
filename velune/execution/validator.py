"""Post-execution state validator checking compiler correctness, paths, and test cases."""

from __future__ import annotations

import logging
import py_compile
from pathlib import Path
from typing import Any

from velune.execution.sandbox import SubprocessSandbox

logger = logging.getLogger("velune.execution.validator")


class ValidationResult:
    """The structured output of a post-execution validation check."""

    def __init__(self, success: bool, errors: list[str], details: dict[str, Any]) -> None:
        self.success = success
        self.errors = errors
        self.details = details

    def __repr__(self) -> str:
        return f"ValidationResult(success={self.success}, errors_count={len(self.errors)})"


class PostExecutionValidator:
    """Validates filesystem expectations, syntax boundaries, and test compilations."""

    def __init__(self, workspace_path: Path, sandbox: SubprocessSandbox | None = None) -> None:
        self.workspace_path = Path(workspace_path).resolve()
        self.sandbox = sandbox or SubprocessSandbox(self.workspace_path)

    def validate(
        self,
        expected_files: list[Path],
        syntax_check_files: list[Path],
        test_command: str | None = None,
        test_timeout: float = 30.0,
    ) -> ValidationResult:
        """Runs the validation rules, reporting all errors."""
        errors: list[str] = []
        details: dict[str, Any] = {}

        # 1. Verify Expected Files exist and are non-empty
        logger.info("Validating presence of expected files...")
        file_checks = {}
        for file in expected_files:
            abs_file = (self.workspace_path / file).resolve()
            if not abs_file.exists():
                errors.append(f"Expected file was not created/found: {file}")
                file_checks[str(file)] = "missing"
            elif abs_file.is_file() and abs_file.stat().st_size == 0:
                errors.append(f"Expected file exists but is empty: {file}")
                file_checks[str(file)] = "empty"
            else:
                file_checks[str(file)] = "ok"
        details["file_checks"] = file_checks

        # 2. Syntax Check for modified source code
        logger.info("Validating language syntax...")
        syntax_checks = {}
        for file in syntax_check_files:
            abs_file = (self.workspace_path / file).resolve()
            if not abs_file.exists() or not abs_file.is_file():
                continue

            if abs_file.suffix == ".py":
                try:
                    py_compile.compile(str(abs_file), doraise=True)
                    syntax_checks[str(file)] = "ok"
                except py_compile.PyCompileError as e:
                    errors.append(f"Python syntax compilation error in {file}:\n{e.msg}")
                    syntax_checks[str(file)] = f"compile_error: {e.msg}"
                except Exception as e:
                    errors.append(f"Unexpected compilation checking failure in {file}: {e}")
                    syntax_checks[str(file)] = f"error: {str(e)}"
            else:
                # Basic non-empty text check or simple braces checks for JS/TS/Go/Rust
                syntax_checks[str(file)] = "skipped (no built-in parser)"
        details["syntax_checks"] = syntax_checks

        # 3. Running Unit Tests in sandbox
        if test_command:
            logger.info("Running post-execution tests: %s", test_command)
            try:
                from velune.core.errors.execution import SandboxError
                from velune.execution.command_spec import CommandSpec

                try:
                    spec = CommandSpec.from_string(
                        test_command, cwd=self.workspace_path, timeout=test_timeout
                    )
                except SandboxError as e:
                    self.sandbox.emit_rejection(test_command, str(e))
                    raise e

                res = self.sandbox.execute(spec)
                details["test_execution"] = res.to_dict()
                if res.exit_code != 0:
                    errors.append(
                        f"Test command '{test_command}' failed with exit code {res.exit_code}.\n"
                        f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
                    )
            except Exception as e:
                errors.append(f"Test command execution failed to run in sandbox: {e}")
                details["test_execution"] = {"error": str(e)}

        success = len(errors) == 0
        if not success:
            logger.error("Validation failed: %d errors detected", len(errors))
        else:
            logger.info("Validation completed successfully with zero errors")

        return ValidationResult(success=success, errors=errors, details=details)
