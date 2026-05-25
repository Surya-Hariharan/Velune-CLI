"""Repository Personality & Style Analyzer Agent.

Introspects Python source code directories via AST parsing to extract coding style conventions,
OOP vs Functional paradigms, naming distributions, type hinting strictness, and docstring formatting.
"""

from __future__ import annotations

import ast
import logging
import os
import re
from typing import Any

logger = logging.getLogger("velune.cognition.personality")


class StyleVisitor(ast.NodeVisitor):
    """AST Visitor to count class/function paradigms, naming conventions, typings, and docstrings."""

    def __init__(self) -> None:
        self.classes_count = 0
        self.functions_count = 0
        self.top_level_functions_count = 0

        # Naming convention counts
        self.snake_case_count = 0
        self.camel_case_count = 0
        self.pascal_case_count = 0
        self.upper_case_count = 0

        # Type annotations counters
        self.annotated_params = 0
        self.total_params = 0
        self.annotated_returns = 0
        self.total_returns = 0

        # Docstring style counters
        self.google_docstrings = 0
        self.sphinx_docstrings = 0
        self.total_docstrings = 0

        # Import modules
        self.imports: set[str] = set()

        # Regex for naming conventions
        self.snake_re = re.compile(r"^[a-z_][a-z0-9_]*$")
        self.camel_re = re.compile(r"^[a-z][a-zA-Z0-9]*$")
        self.pascal_re = re.compile(r"^[A-Z][a-zA-Z0-9]*$")
        self.upper_re = re.compile(r"^[A-Z_][A-Z0-9_]*$")

    def _classify_name(self, name: str) -> None:
        if not name or name.startswith("__") and name.endswith("__"):
            return

        if self.snake_re.match(name):
            self.snake_case_count += 1
        elif self.camel_re.match(name):
            self.camel_case_count += 1
        elif self.pascal_re.match(name):
            self.pascal_case_count += 1
        elif self.upper_re.match(name):
            self.upper_case_count += 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes_count += 1
        self._classify_name(node.name)

        # Check class docstring
        doc = ast.get_docstring(node)
        if doc:
            self._analyze_docstring(doc)

        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.functions_count += 1
        self._classify_name(node.name)

        # Check if function is top-level (module level)
        # Note: In NodeVisitor, we can track parent, but a simple check is to count total and we know if it's OOP dominant or Functional
        # Let's count parameters and return annotations
        # Exclude 'self' or 'cls' parameter
        for arg in node.args.args:
            if arg.arg in ("self", "cls"):
                continue
            self.total_params += 1
            if arg.annotation is not None:
                self.annotated_params += 1

        for arg in node.args.kwonlyargs:
            self.total_params += 1
            if arg.annotation is not None:
                self.annotated_params += 1

        if node.args.vararg:
            self.total_params += 1
            if node.args.vararg.annotation is not None:
                self.annotated_params += 1

        if node.args.kwarg:
            self.total_params += 1
            if node.args.kwarg.annotation is not None:
                self.annotated_params += 1

        self.total_returns += 1
        if node.returns is not None:
            self.annotated_returns += 1

        # Check docstring
        doc = ast.get_docstring(node)
        if doc:
            self._analyze_docstring(doc)

        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self._classify_name(node.id)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            parts = alias.name.split(".")
            self.imports.add(parts[0])
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module:
            parts = node.module.split(".")
            self.imports.add(parts[0])
        self.generic_visit(node)

    def _analyze_docstring(self, doc: str) -> None:
        self.total_docstrings += 1

        # Check for Google Style
        if "Args:" in doc or "Returns:" in doc or "Yields:" in doc:
            self.google_docstrings += 1
        # Check for Sphinx Style
        elif ":param" in doc or ":type" in doc or ":return:" in doc:
            self.sphinx_docstrings += 1


class RepositoryPersonalityAgent:
    """Introspects Python directories to compile a cohesive model coding personality style."""

    def __init__(self, workspace_root: str | None = None) -> None:
        self.workspace_root = workspace_root or os.getcwd()

    def analyze_directory_style(self, directory: str) -> dict[str, Any]:
        """Recursively scan Python files in directory via AST and compile the style profile."""
        visitor = StyleVisitor()

        # Count top-level functions (directly under Module)
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith(".py"):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, encoding="utf-8", errors="ignore") as f:
                            code = f.read()
                        tree = ast.parse(code)
                        visitor.visit(tree)

                        # Inspect module body for top-level functions
                        for node in tree.body:
                            if isinstance(node, ast.FunctionDef):
                                visitor.top_level_functions_count += 1
                    except Exception:
                        pass

        # Calculate naming conventions
        naming_totals = (
            visitor.snake_case_count +
            visitor.camel_case_count +
            visitor.pascal_case_count +
            visitor.upper_case_count
        )

        naming_stats = {
            "snake_case": visitor.snake_case_count,
            "camelCase": visitor.camel_case_count,
            "PascalCase": visitor.pascal_case_count,
            "UPPER_CASE": visitor.upper_case_count,
        }

        dominant_naming = "Hybrid"
        if naming_totals > 0:
            for k, v in naming_stats.items():
                ratio = v / naming_totals
                if ratio >= 0.70:
                    dominant_naming = k
                    break

        # Calculate type hinting strictness
        total_annotations = visitor.total_params + visitor.total_returns
        annotated_total = visitor.annotated_params + visitor.annotated_returns
        type_strictness = round(annotated_total / total_annotations, 3) if total_annotations > 0 else 1.0

        # Calculate OOP vs Functional Paradigm
        # If classes are dominant,OOP. If top-level functions are dominant, Functional. Else hybrid.
        classes = visitor.classes_count
        top_funcs = visitor.top_level_functions_count

        if classes > top_funcs * 2 and classes > 1:
            class_vs_functional = "OOP"
        elif top_funcs > classes * 2 and top_funcs > 1:
            class_vs_functional = "Functional"
        else:
            class_vs_functional = "Hybrid"

        # Calculate docstring style
        if visitor.total_docstrings > 0:
            if visitor.google_docstrings > visitor.sphinx_docstrings:
                docstring_style = "Google"
            elif visitor.sphinx_docstrings > visitor.google_docstrings:
                docstring_style = "Sphinx"
            else:
                docstring_style = "Google"  # Default fallback
        else:
            docstring_style = "Google"

        # Filter external packages/preferred constructs (exclude standard short names or project local parts if wanted)
        preferred = sorted(list(visitor.imports))[:8]

        return {
            "naming_conventions": {
                "dominant": dominant_naming,
                "breakdown": naming_stats,
            },
            "type_hinting_strictness": type_strictness,
            "preferred_constructs": preferred,
            "class_vs_functional": class_vs_functional,
            "docstring_style": docstring_style,
        }
