"""Python linter using only the standard library (ast + py_compile).

Surfaces three classes of issue without requiring flake8 or pylint:
  E001 — SyntaxError  (parse failure)
  W001 — Unused import (imported name never referenced)
  W002 — Complex function (branch count > COMPLEXITY_THRESHOLD)
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

COMPLEXITY_THRESHOLD = 10  # branches before W002 fires


@dataclass
class LintDiagnostic:
    file_path: str
    line: int
    col: int
    severity: str  # "error" | "warning"
    code: str  # "E001", "W001", "W002"
    message: str


# ---------------------------------------------------------------------------
# Internal visitors
# ---------------------------------------------------------------------------


class _ImportChecker(ast.NodeVisitor):
    """Detect imported names that are never referenced (W001)."""

    def __init__(self) -> None:
        self._imported: list[tuple[str, int]] = []  # (name, lineno)
        self._used: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            local_name = alias.asname if alias.asname else alias.name.split(".")[0]
            self._imported.append((local_name, node.lineno))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        for alias in node.names:
            if alias.name == "*":
                return  # star-imports — can't tell what's used
            local_name = alias.asname if alias.asname else alias.name
            self._imported.append((local_name, node.lineno))

    def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
        if isinstance(node.ctx, ast.Load):
            self._used.add(node.id)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        # Record the root name (e.g. `os` in `os.path.join`)
        root = node
        while isinstance(root, ast.Attribute):
            root = root.value
        if isinstance(root, ast.Name):
            self._used.add(root.id)
        self.generic_visit(node)

    def unused(self) -> list[tuple[str, int]]:
        return [(name, line) for name, line in self._imported if name not in self._used]


class _ComplexityVisitor(ast.NodeVisitor):
    """Count decision points per function to detect overly complex code (W002)."""

    _BRANCH_NODES = (
        ast.If,
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.ExceptHandler,
        ast.With,
        ast.AsyncWith,
        ast.Assert,
    )

    def __init__(self) -> None:
        self.complex: list[tuple[str, int, int]] = []  # (name, lineno, count)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._check(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _check(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        count = sum(1 for child in ast.walk(node) if isinstance(child, self._BRANCH_NODES))
        if count > COMPLEXITY_THRESHOLD:
            self.complex.append((node.name, node.lineno, count))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class PythonLinter:
    """Lint Python source using only stdlib — no external dependencies."""

    def lint_source(self, source: str, file_path: str = "<unknown>") -> list[LintDiagnostic]:
        diags: list[LintDiagnostic] = []

        # ── E001: syntax ────────────────────────────────────────────────
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError as exc:
            diags.append(
                LintDiagnostic(
                    file_path=file_path,
                    line=exc.lineno or 1,
                    col=exc.offset or 0,
                    severity="error",
                    code="E001",
                    message=f"SyntaxError: {exc.msg}",
                )
            )
            return diags  # can't do further analysis without a tree

        # ── W001: unused imports ─────────────────────────────────────────
        import_checker = _ImportChecker()
        import_checker.visit(tree)
        for name, lineno in import_checker.unused():
            diags.append(
                LintDiagnostic(
                    file_path=file_path,
                    line=lineno,
                    col=0,
                    severity="warning",
                    code="W001",
                    message=f"Unused import: '{name}'",
                )
            )

        # ── W002: complex function ──────────────────────────────────────
        complexity_visitor = _ComplexityVisitor()
        complexity_visitor.visit(tree)
        for fn_name, lineno, count in complexity_visitor.complex:
            diags.append(
                LintDiagnostic(
                    file_path=file_path,
                    line=lineno,
                    col=0,
                    severity="warning",
                    code="W002",
                    message=(
                        f"Function '{fn_name}' has {count} branches "
                        f"(threshold: {COMPLEXITY_THRESHOLD}) — consider splitting"
                    ),
                )
            )

        diags.sort(key=lambda d: (d.line, d.col))
        return diags

    def lint_file(self, path: Path) -> list[LintDiagnostic]:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return [
                LintDiagnostic(
                    file_path=str(path),
                    line=0,
                    col=0,
                    severity="error",
                    code="E000",
                    message=f"Cannot read file: {exc}",
                )
            ]
        return self.lint_source(source, str(path))


def render_lint_panel(console, filename: str, diagnostics: list[LintDiagnostic]) -> None:
    """Render lint diagnostics to the Rich console before the LLM call."""
    from rich.panel import Panel

    from velune.cli import design

    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]

    lines: list[str] = []
    for d in diagnostics:
        color = design.DANGER if d.severity == "error" else design.WARN
        lines.append(
            f"[{color}]{d.severity.upper()}[/{color}] "
            f"[dim]{d.line}:{d.col}[/dim] "
            f"[bold]{d.code}[/bold] {d.message}"
        )

    body = "\n".join(lines)
    title_color = design.DANGER if errors else design.WARN
    title = (
        f"[{title_color}]Lint · {filename}[/{title_color}]  "
        f"[dim]{len(errors)} error(s), {len(warnings)} warning(s)[/dim]"
    )
    console.print(Panel(body, title=title, border_style="dim", padding=(0, 1)))
