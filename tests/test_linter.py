"""Tests for velune.analysis.linter.PythonLinter."""

from __future__ import annotations

import pytest

from velune.analysis.linter import LintDiagnostic, PythonLinter


@pytest.fixture()
def linter():
    return PythonLinter()


class TestPythonLinter:
    def test_valid_source_no_errors(self, linter):
        src = "def foo(x):\n    return x + 1\n"
        diags = linter.lint_source(src, "test.py")
        assert diags == []

    def test_syntax_error_detected(self, linter):
        src = "def foo(\n    pass\n"
        diags = linter.lint_source(src, "test.py")
        errors = [d for d in diags if d.code == "E001"]
        assert errors, "Expected E001 for syntax error"
        assert errors[0].severity == "error"

    def test_syntax_error_has_line_number(self, linter):
        src = "x = (\n  1 +\n"
        diags = linter.lint_source(src, "test.py")
        assert any(d.code == "E001" for d in diags)
        e = next(d for d in diags if d.code == "E001")
        assert e.line >= 1

    def test_unused_import_warning(self, linter):
        src = "import os\nimport sys\n\ndef foo():\n    return sys.argv\n"
        diags = linter.lint_source(src, "test.py")
        codes = [d.code for d in diags]
        assert "W001" in codes
        w = next(d for d in diags if d.code == "W001")
        assert "os" in w.message

    def test_used_import_not_flagged(self, linter):
        src = "import os\n\ndef foo():\n    return os.getcwd()\n"
        diags = linter.lint_source(src, "test.py")
        assert not any(d.code == "W001" for d in diags)

    def test_complex_function_warning(self, linter):
        branches = "\n".join(f"    if x == {i}:" + "\n        pass" for i in range(12))
        src = f"def big(x):\n{branches}\n"
        diags = linter.lint_source(src, "test.py")
        assert any(d.code == "W002" for d in diags)

    def test_simple_function_no_complexity_warning(self, linter):
        src = "def foo(x):\n    if x:\n        return 1\n    return 0\n"
        diags = linter.lint_source(src, "test.py")
        assert not any(d.code == "W002" for d in diags)

    def test_empty_source_no_errors(self, linter):
        assert linter.lint_source("", "test.py") == []

    def test_lint_file_reads_from_disk(self, linter, tmp_path):
        f = tmp_path / "sample.py"
        f.write_text("import re\n\ndef ok():\n    return 1\n")
        diags = linter.lint_file(f)
        assert any(d.code == "W001" and "re" in d.message for d in diags)

    def test_lint_file_missing_returns_error(self, linter, tmp_path):
        diags = linter.lint_file(tmp_path / "nonexistent.py")
        assert diags[0].code == "E000"

    def test_diagnostics_sorted_by_line(self, linter):
        # Two unused imports at different lines
        src = "import os\nimport sys\n\ndef foo(): pass\n"
        diags = linter.lint_source(src, "t.py")
        lines = [d.line for d in diags]
        assert lines == sorted(lines)


@pytest.mark.parametrize(
    "source,expected_code",
    [
        ("def foo(\n    pass", "E001"),
        ("import pathlib\ndef f(): pass", "W001"),
    ],
)
def test_parametrized_diagnostics(source, expected_code):
    diags = PythonLinter().lint_source(source, "t.py")
    assert any(d.code == expected_code for d in diags)
