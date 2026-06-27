"""Type hint inference for Python source using only the standard ast module.

TypeInferrer walks every FunctionDef/AsyncFunctionDef, skips ones that are
already fully annotated, and infers simple type hints from:
  - return statement constants and container literals
  - default argument values (bool/int/str/None/list)

The output is a unified diff string suitable for display, and an apply_suggestions()
method that patches the source in-place.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass
from pathlib import Path

# Map from default-value node types to suggested type strings
_DEFAULT_TYPE_MAP: dict[type, str] = {
    ast.Constant: "",  # handled specially below
}


@dataclass
class TypeSuggestion:
    function_name: str
    line: int  # 1-based line of the def statement
    param_suggestions: dict[str, str]  # param_name -> type hint string
    return_suggestion: str | None
    confidence: str  # "high" | "medium" | "low"


class TypeInferrer:
    """Infer missing type annotations for Python functions."""

    def infer_source(self, source: str, file_path: str = "<unknown>") -> list[TypeSuggestion]:
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError:
            return []

        suggestions: list[TypeSuggestion] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                sug = self._analyse_function(node)
                if sug is not None:
                    suggestions.append(sug)

        suggestions.sort(key=lambda s: s.line)
        return suggestions

    def infer_file(self, path: Path) -> list[TypeSuggestion]:
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        return self.infer_source(source, str(path))

    # ------------------------------------------------------------------
    # Diff / patch helpers
    # ------------------------------------------------------------------

    def _render_suggestions(self, source: str, suggestions: list[TypeSuggestion]) -> str:
        """Return a unified-diff string showing the proposed annotation changes."""
        patched = self.apply_suggestions(source, suggestions)
        original_lines = source.splitlines(keepends=True)
        patched_lines = patched.splitlines(keepends=True)
        diff = list(
            difflib.unified_diff(
                original_lines,
                patched_lines,
                fromfile="original",
                tofile="with type hints",
                lineterm="",
            )
        )
        return "".join(diff) if diff else "(no changes — all annotations already present)"

    def apply_suggestions(self, source: str, suggestions: list[TypeSuggestion]) -> str:
        """Patch *source* by adding the suggested annotations.

        Works line-by-line so it does not require re-parsing the AST.
        Processes functions from bottom to top to keep line numbers stable.
        """
        lines = source.split("\n")
        for sug in sorted(suggestions, key=lambda s: -s.line):
            idx = sug.line - 1  # convert to 0-based
            if idx < 0 or idx >= len(lines):
                continue
            line = lines[idx]
            line = self._patch_return(line, sug.return_suggestion)
            line = self._patch_params(line, sug.param_suggestions)
            lines[idx] = line
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Private analysis helpers
    # ------------------------------------------------------------------

    def _analyse_function(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> TypeSuggestion | None:
        param_sug: dict[str, str] = {}
        return_sug: str | None = None
        confidence = "medium"

        # ── return type ──────────────────────────────────────────────────
        if node.returns is None:
            inferred = self._infer_return_type(node)
            if inferred is not None:
                return_sug = inferred
                confidence = "high" if inferred not in ("None", "Any") else "medium"

        # ── parameter types ──────────────────────────────────────────────
        args = node.args
        all_args = list(args.posonlyargs) + list(args.args)
        defaults_offset = len(all_args) - len(args.defaults)

        for i, arg in enumerate(all_args):
            if arg.arg in ("self", "cls"):
                continue
            if arg.annotation is not None:
                continue  # already annotated
            default_idx = i - defaults_offset
            default_node = args.defaults[default_idx] if default_idx >= 0 else None
            inferred_param = self._infer_param_type(default_node)
            if inferred_param is not None:
                param_sug[arg.arg] = inferred_param

        if not param_sug and return_sug is None:
            return None

        return TypeSuggestion(
            function_name=node.name,
            line=node.lineno,
            param_suggestions=param_sug,
            return_suggestion=return_sug,
            confidence=confidence,
        )

    def _infer_return_type(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
        types: set[str] = set()

        # Detect generator functions first
        for child in ast.walk(node):
            if isinstance(child, ast.Yield | ast.YieldFrom):
                return "Generator"

        # Collect return types from return statements
        for child in ast.walk(node):
            if isinstance(child, ast.Return):
                if child.value is None:
                    types.add("None")
                else:
                    t = self._type_of_expr(child.value)
                    if t:
                        types.add(t)

        if not types:
            return "None"  # implicit None return

        types.discard("")
        if not types:
            return None
        if len(types) == 1:
            return next(iter(types))
        # Multiple return types → union
        return " | ".join(sorted(types))

    def _infer_param_type(self, default: ast.expr | None) -> str | None:
        if default is None:
            return None
        return self._type_of_expr(default)

    def _type_of_expr(self, node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant):
            if node.value is None:
                return "None"
            if isinstance(node.value, bool):
                return "bool"
            if isinstance(node.value, int):
                return "int"
            if isinstance(node.value, float):
                return "float"
            if isinstance(node.value, str):
                return "str"
            if isinstance(node.value, bytes):
                return "bytes"
        if isinstance(node, ast.List):
            return "list"
        if isinstance(node, ast.ListComp):
            return "list"
        if isinstance(node, ast.Dict):
            return "dict"
        if isinstance(node, ast.DictComp):
            return "dict"
        if isinstance(node, ast.Set):
            return "set"
        if isinstance(node, ast.SetComp):
            return "set"
        if isinstance(node, ast.Tuple):
            return "tuple"
        if isinstance(node, ast.JoinedStr):
            return "str"
        return None

    # ------------------------------------------------------------------
    # Line-level patching
    # ------------------------------------------------------------------

    def _patch_return(self, line: str, return_type: str | None) -> str:
        """Add `-> <type>:` to a def line that has no return annotation."""
        if return_type is None:
            return line
        # Only patch lines that look like `def name(...):`
        stripped = line.rstrip()
        if not re.search(r"\)\s*:", stripped):
            return line
        if "->" in stripped:
            return line
        patched = re.sub(r"\)\s*:(\s*)$", f") -> {return_type}:\\1", stripped)
        return patched

    def _patch_params(self, line: str, param_sug: dict[str, str]) -> str:
        """Add type annotations to individual parameters on a def line."""
        if not param_sug:
            return line
        for param, hint in param_sug.items():
            # Match param name followed by optional =default, no existing annotation
            # e.g.  `param,`  or  `param=val,`  or  `param=val)`
            pattern = rf"\b({re.escape(param)})(\s*(?:=|,|\)))"

            def _replace(m: re.Match, _line: str = line, _hint: str = hint) -> str:
                if ":" in _line[: m.start()]:
                    return m.group(0)  # already has an annotation earlier
                return f"{m.group(1)}: {_hint}{m.group(2)}"

            line = re.sub(pattern, _replace, line, count=1)
        return line
