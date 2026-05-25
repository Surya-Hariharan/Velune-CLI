"""Self-Reflection & Reasoning Verification Pipeline.

Audits proposed modifications and plan outputs to guarantee architectural
conformity, signature correctness, and import existence.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
from typing import Any


class ReasoningVerifier:
    """
    Self-reflection and reasoning verification pipeline to analyze generated
    code patches, plans, and files prior to execution to prevent architectural
    regressions and syntax issues.
    """

    def __init__(self, workspace_root: str | None = None) -> None:
        self.workspace_root = workspace_root or os.getcwd()

    def analyze_proposed_imports(self, proposed_code: str) -> dict[str, Any]:
        """
        Detects potential hallucinated imports in proposed Python code.
        Checks if standard libraries, installed packages, or local workspace modules exist.
        """
        issues = []
        try:
            tree = ast.parse(proposed_code)
        except SyntaxError as e:
            return {
                "success": False,
                "issues": [f"Syntax Error in proposed code: {e.msg} at line {e.lineno}"],
            }

        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    imported_modules.add(name.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module.split('.')[0])

        for mod_name in imported_modules:
            # 1. Check if it's standard library or currently imported
            if mod_name in sys.builtin_module_names:
                continue

            # Try to resolve module spec
            spec = None
            try:
                spec = importlib.util.find_spec(mod_name)
            except Exception:
                pass

            if spec is not None:
                continue

            # 2. Check if it's a local package or module in the workspace
            local_path_dir = os.path.join(self.workspace_root, mod_name)
            local_path_file = os.path.join(self.workspace_root, f"{mod_name}.py")
            if os.path.isdir(local_path_dir) or os.path.isfile(local_path_file):
                continue

            issues.append(f"Potential hallucinated import: module '{mod_name}' could not be resolved.")

        return {
            "success": len(issues) == 0,
            "issues": issues,
        }

    def analyze_contradictions(self, proposed_code: str, existing_code: str) -> dict[str, Any]:
        """
        Compares proposed code against existing code to identify signature mismatches,
        duplicate function/class definitions, or structural contradictions.
        """
        issues = []
        try:
            proposed_tree = ast.parse(proposed_code)
        except SyntaxError as e:
            return {
                "success": False,
                "issues": [f"Syntax Error in proposed code: {e.msg} at line {e.lineno}"],
            }

        try:
            existing_tree = ast.parse(existing_code) if existing_code else None
        except SyntaxError:
            existing_tree = None

        if not existing_tree:
            return {"success": True, "issues": []}

        # Extract functions/classes from both trees
        def extract_definitions(tree: ast.AST) -> dict[str, Any]:
            defs: dict[str, Any] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    args_count = len(node.args.args)
                    defs[node.name] = {
                        "type": "function",
                        "args_count": args_count,
                        "args": [arg.arg for arg in node.args.args],
                    }
                elif isinstance(node, ast.ClassDef):
                    methods = {}
                    for subnode in node.body:
                        if isinstance(subnode, ast.FunctionDef):
                            args_count = len(subnode.args.args)
                            methods[subnode.name] = {
                                "args_count": args_count,
                                "args": [arg.arg for arg in subnode.args.args],
                            }
                    defs[node.name] = {
                        "type": "class",
                        "methods": methods,
                    }
            return defs

        proposed_defs = extract_definitions(proposed_tree)
        existing_defs = extract_definitions(existing_tree)

        # Check for contradictions
        for name, p_def in proposed_defs.items():
            if name in existing_defs:
                e_def = existing_defs[name]
                if p_def["type"] != e_def["type"]:
                    issues.append(
                        f"Type contradiction: '{name}' is defined as a {p_def['type']} "
                        f"in proposed code but is a {e_def['type']} in existing code."
                    )
                elif p_def["type"] == "function":
                    if p_def["args_count"] != e_def["args_count"]:
                        issues.append(
                            f"Signature mismatch in function '{name}': proposed code has "
                            f"{p_def['args_count']} arguments {p_def['args']}, but existing "
                            f"code has {e_def['args_count']} arguments {e_def['args']}."
                        )
                elif p_def["type"] == "class":
                    p_methods = p_def["methods"]
                    e_methods = e_def["methods"]
                    for m_name, p_method in p_methods.items():
                        if m_name in e_methods:
                            e_method = e_methods[m_name]
                            if p_method["args_count"] != e_method["args_count"]:
                                issues.append(
                                    f"Signature mismatch in method '{name}.{m_name}': proposed "
                                    f"code has {p_method['args_count']} arguments {p_method['args']}, "
                                    f"but existing code has {e_method['args_count']} arguments {e_method['args']}."
                                )

        return {
            "success": len(issues) == 0,
            "issues": issues,
        }

    def audit_patch(self, file_path: str, proposed_code: str, existing_code: str) -> dict[str, Any]:
        """
        Runs complete contradiction, import hallucination, and general syntax audits.
        """
        is_python = file_path.endswith(".py")

        if not is_python:
            return {
                "passed": True,
                "issues": [],
                "file_path": file_path,
                "critique": "Static validation skipped for non-python file.",
            }

        import_results = self.analyze_proposed_imports(proposed_code)
        contradiction_results = self.analyze_contradictions(proposed_code, existing_code)

        all_issues = import_results.get("issues", []) + contradiction_results.get("issues", [])
        passed = len(all_issues) == 0

        # Construct structured critique
        critique_lines = [
            f"Audit for patch to '{file_path}':",
            f"Status: {'PASSED' if passed else 'FAILED'}",
        ]
        if all_issues:
            critique_lines.append("Issues found:")
            for issue in all_issues:
                critique_lines.append(f"- {issue}")
        else:
            critique_lines.append("No architectural or signature contradictions detected.")

        critique_lines.append("\nAssess this patch for architectural regression. List potential failure vectors.")

        return {
            "passed": passed,
            "issues": all_issues,
            "file_path": file_path,
            "critique": "\n".join(critique_lines),
        }
