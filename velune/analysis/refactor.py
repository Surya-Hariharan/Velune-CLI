"""Code-smell detectors built on Python's standard ast module.

Each detector is an independent ast.NodeVisitor subclass so they can be
run and tested in isolation.  RefactorAnalyzer orchestrates them all.

Rules:
  R001 — Long function    (body > 50 lines)
  R002 — Too many params  (> 5 parameters, excluding *args/**kwargs)
  R003 — Deep nesting     (nesting depth > 4)
  R004 — Mutable default  (def f(x=[]) or def f(x={}))
  R005 — Bare except      (except: without an exception type)
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

LONG_FUNCTION_LINES = 50
MAX_PARAMS = 5
MAX_NESTING_DEPTH = 4


@dataclass
class RefactorHint:
    rule_id: str  # "R001" … "R005"
    function_name: str | None
    line: int
    severity: str  # "warning" | "error"
    message: str
    suggestion: str


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


class LongFunctionDetector(ast.NodeVisitor):
    """R001: Flag functions whose bodies span more than LONG_FUNCTION_LINES lines."""

    def __init__(self, hints: list[RefactorHint]) -> None:
        self._hints = hints

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._check(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _check(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        body_lines = (node.end_lineno or node.lineno) - node.lineno
        if body_lines > LONG_FUNCTION_LINES:
            self._hints.append(
                RefactorHint(
                    rule_id="R001",
                    function_name=node.name,
                    line=node.lineno,
                    severity="warning",
                    message=f"Function '{node.name}' spans {body_lines} lines",
                    suggestion=f"Split into smaller helpers (target < {LONG_FUNCTION_LINES} lines each)",
                )
            )


class DuplicateArgumentsDetector(ast.NodeVisitor):
    """R002: Flag functions with more than MAX_PARAMS regular parameters."""

    def __init__(self, hints: list[RefactorHint]) -> None:
        self._hints = hints

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._check(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _check(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Count only positional/keyword params — exclude self/cls, *args, **kwargs
        args = node.args
        params = [a for a in args.args if a.arg not in ("self", "cls")]
        params += list(args.posonlyargs)
        count = len(params)
        if count > MAX_PARAMS:
            self._hints.append(
                RefactorHint(
                    rule_id="R002",
                    function_name=node.name,
                    line=node.lineno,
                    severity="warning",
                    message=f"Function '{node.name}' has {count} parameters",
                    suggestion="Group related params into a dataclass or config object",
                )
            )


class DeepNestingDetector(ast.NodeVisitor):
    """R003: Flag functions where control-flow nesting exceeds MAX_NESTING_DEPTH."""

    _NESTING_NODES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try)

    def __init__(self, hints: list[RefactorHint]) -> None:
        self._hints = hints

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        max_depth = self._max_depth(node.body, 0)
        if max_depth > MAX_NESTING_DEPTH:
            self._hints.append(
                RefactorHint(
                    rule_id="R003",
                    function_name=node.name,
                    line=node.lineno,
                    severity="warning",
                    message=f"Function '{node.name}' nests {max_depth} levels deep",
                    suggestion="Extract inner blocks into helper functions or invert conditions (early return)",
                )
            )
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _max_depth(self, stmts: list[ast.stmt], current: int) -> int:
        max_d = current
        for stmt in stmts:
            if isinstance(stmt, self._NESTING_NODES):
                child_depth = current + 1
                for _field, value in ast.iter_fields(stmt):
                    if isinstance(value, list):
                        sub = self._max_depth(value, child_depth)
                        if sub > max_d:
                            max_d = sub
                    elif isinstance(value, ast.AST):
                        sub = self._max_depth([value], child_depth)  # type: ignore[list-item]
                        if sub > max_d:
                            max_d = sub
            else:
                max_d = max(max_d, current)
        return max_d


class MutableDefaultArgumentDetector(ast.NodeVisitor):
    """R004: Flag functions with mutable default argument values (list/dict/set literals)."""

    _MUTABLE = (ast.List, ast.Dict, ast.Set)

    def __init__(self, hints: list[RefactorHint]) -> None:
        self._hints = hints

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._check(node)
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _check(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        defaults = node.args.defaults + node.args.kw_defaults
        for default in defaults:
            if default is not None and isinstance(default, self._MUTABLE):
                kind = type(default).__name__.lower()
                self._hints.append(
                    RefactorHint(
                        rule_id="R004",
                        function_name=node.name,
                        line=node.lineno,
                        severity="warning",
                        message=f"Function '{node.name}' uses a mutable {kind} as a default argument",
                        suggestion="Replace with None and initialise inside the function body",
                    )
                )
                break  # one hint per function is enough


class BareExceptDetector(ast.NodeVisitor):
    """R005: Flag bare `except:` clauses that catch everything including BaseException."""

    def __init__(self, hints: list[RefactorHint]) -> None:
        self._hints = hints

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:  # noqa: N802
        if node.type is None:
            # Walk up to find the enclosing function name (best-effort)
            self._hints.append(
                RefactorHint(
                    rule_id="R005",
                    function_name=None,
                    line=node.lineno,
                    severity="warning",
                    message="Bare `except:` catches all exceptions including SystemExit and KeyboardInterrupt",
                    suggestion="Catch a specific exception type, e.g. `except ValueError:`",
                )
            )
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class RefactorAnalyzer:
    """Run all detectors over a file or source string and collect hints."""

    _DETECTOR_CLASSES = [
        LongFunctionDetector,
        DuplicateArgumentsDetector,
        DeepNestingDetector,
        MutableDefaultArgumentDetector,
        BareExceptDetector,
    ]

    def analyze_source(self, source: str, file_path: str = "<unknown>") -> list[RefactorHint]:
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError:
            return []  # can't analyse unparseable source

        hints: list[RefactorHint] = []
        for cls in self._DETECTOR_CLASSES:
            detector = cls(hints)
            detector.visit(tree)

        hints.sort(key=lambda h: h.line)
        return hints

    def analyze_file(self, path: Path) -> list[RefactorHint]:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return self.analyze_source(source, str(path))
