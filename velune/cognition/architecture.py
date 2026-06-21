"""Architecture Cognition Agent and Technical Debt Ledger."""

from __future__ import annotations

import ast
import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger("velune.cognition.architecture")


class ArchitectureDriftAlarm(Exception):
    """Exception raised when an architectural layering boundary rule is violated."""

    pass


class LCOMVisitor(ast.NodeVisitor):
    """AST visitor to calculate Lack of Cohesion of Methods (LCOM4/LCOM)."""

    def __init__(self) -> None:
        self.current_method: str | None = None
        self.method_accessed_attributes: dict[str, set[str]] = {}
        self.class_name: str | None = None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_name = node.name
        # Process the body of the class
        for subnode in node.body:
            if isinstance(subnode, ast.FunctionDef):
                self.current_method = subnode.name
                self.method_accessed_attributes[subnode.name] = set()
                self.generic_visit(subnode)
                self.current_method = None
        # Do not recurse into nested classes to avoid confusion

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Check if attribute access is on 'self' (e.g. self.attr)
        if self.current_method and isinstance(node.value, ast.Name) and node.value.id == "self":
            self.method_accessed_attributes[self.current_method].add(node.attr)
        self.generic_visit(node)


class CognitiveDebtLedger:
    """Manages the persistent registry of technical debt, coupling, and modularity violations."""

    def __init__(self, ledger_path: str | Path | None = None) -> None:
        if ledger_path is None:
            self.ledger_path = Path(".velune") / "debt_ledger.json"
        else:
            self.ledger_path = Path(ledger_path)

        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self._load_ledger()

    def _load_ledger(self) -> None:
        if self.ledger_path.exists():
            try:
                with open(self.ledger_path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {"debt_items": [], "total_severity": 0.0}
        else:
            self.data = {"debt_items": [], "total_severity": 0.0}

    def _save_ledger(self) -> None:
        try:
            with open(self.ledger_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception as e:
            logger.error("Failed to write to technical debt ledger: %s", e)

    def add_debt_item(
        self, file_path: str, category: str, description: str, severity: float
    ) -> None:
        """Add or update an item in the debt ledger."""
        # Check if duplicate exists
        for item in self.data["debt_items"]:
            if (
                item["file_path"] == file_path
                and item["category"] == category
                and item["description"] == description
            ):
                item["severity"] = severity
                item["updated_at"] = (
                    os.path.getmtime(str(self.ledger_path)) if self.ledger_path.exists() else 0.0
                )
                break
        else:
            self.data["debt_items"].append(
                {
                    "file_path": file_path,
                    "category": category,
                    "description": description,
                    "severity": severity,
                }
            )

        # Recompute total severity
        self.data["total_severity"] = sum(item["severity"] for item in self.data["debt_items"])
        self._save_ledger()

    def clear_file_debt(self, file_path: str) -> None:
        """Clear all debt items registered for a specific file."""
        self.data["debt_items"] = [
            item for item in self.data["debt_items"] if item["file_path"] != file_path
        ]
        self.data["total_severity"] = sum(item["severity"] for item in self.data["debt_items"])
        self._save_ledger()

    def get_items(self) -> list[dict[str, Any]]:
        return self.data["debt_items"]


class ArchitectureCognitionAgent:
    """
    Analyzes repository structure, calculates cohesion metrics (LCOM),
    verifies architectural boundary coupling, and records technical debt.
    """

    def __init__(
        self, workspace_root: str | None = None, ledger: CognitiveDebtLedger | None = None
    ) -> None:
        self.workspace_root = workspace_root or os.getcwd()
        self.ledger = ledger or CognitiveDebtLedger()

        # Layering Rules: maps prefix -> list of disallowed module prefixes
        self.layering_rules: list[tuple[str, list[str]]] = [
            ("velune/core", ["velune/execution", "velune/cognition", "velune/cli"]),
            ("velune/kernel", ["velune/execution", "velune/cognition", "velune/cli"]),
            ("velune/models", ["velune/providers", "velune/orchestration", "velune/cli"]),
        ]

    def calculate_lcom(self, code: str) -> dict[str, int]:
        """
        Calculates Lack of Cohesion of Methods (LCOM) for each class in the code.
        Returns a dictionary mapping class name to LCOM score.
        """
        scores: dict[str, int] = {}
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return scores

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                visitor = LCOMVisitor()
                visitor.visit(node)

                methods = list(visitor.method_accessed_attributes.keys())
                # Exclude constructor __init__ from scoring if preferred, or keep all.
                # Let's count LCOM based on standard formula.
                p = 0
                q = 0

                n = len(methods)
                if n <= 1:
                    scores[node.name] = 0
                    continue

                for i in range(n):
                    for j in range(i + 1, n):
                        m1 = methods[i]
                        m2 = methods[j]
                        s1 = visitor.method_accessed_attributes[m1]
                        s2 = visitor.method_accessed_attributes[m2]
                        if s1.isdisjoint(s2):
                            p += 1
                        else:
                            q += 1

                lcom = p - q if p > q else 0
                scores[node.name] = lcom

        return scores

    def calculate_coupling_ratio(self, directory: str) -> float:
        """
        Calculates coupling ratio recursively over a directory's python files:
        external_project_imports / (internal_project_imports + external_project_imports).
        Project imports are identified by a 'velune' module prefix.
        """
        abs_dir = os.path.abspath(directory)
        parts = Path(abs_dir).parts
        if "velune" in parts:
            idx = parts.index("velune")
            rel_parts = parts[idx:]
            prefix = ".".join(rel_parts)
        else:
            prefix = "velune." + os.path.basename(abs_dir)

        internal_count = 0
        external_count = 0

        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith(".py"):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        tree = ast.parse(content)
                        for node in ast.walk(tree):
                            imported_mods = []
                            if isinstance(node, ast.Import):
                                for name in node.names:
                                    imported_mods.append(name.name)
                            elif isinstance(node, ast.ImportFrom) and node.module:
                                imported_mods.append(node.module)

                            for imported_mod in imported_mods:
                                if imported_mod == "velune" or imported_mod.startswith("velune."):
                                    if imported_mod.startswith(prefix):
                                        internal_count += 1
                                    else:
                                        external_count += 1
                    except Exception:
                        pass

        total = internal_count + external_count
        if total == 0:
            return 0.0
        return round(external_count / total, 3)

    def calculate_shi(self, directory: str) -> float:
        """
        Calculates the Subsystem Health Index (SHI) for the given directory.
        SHI(M) = max(0.0, min(1.0, 1.0 * Cohesion - 0.5 * Coupling - 0.2 * DebtPenalty))
        """
        # Calculate Average LCOM
        lcom_scores = []
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith(".py"):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, encoding="utf-8", errors="ignore") as f:
                            content = f.read()
                        scores = self.calculate_lcom(content)
                        lcom_scores.extend(scores.values())
                    except Exception:
                        pass

        if lcom_scores:
            avg_lcom = sum(lcom_scores) / len(lcom_scores)
            cohesion = 1.0 - min(avg_lcom / 10.0, 1.0)
        else:
            cohesion = 1.0

        coupling = self.calculate_coupling_ratio(directory)

        # Calculate Debt Penalty
        norm_dir = os.path.abspath(directory)
        sum_severity = 0.0
        for item in self.ledger.get_items():
            item_path = os.path.abspath(item["file_path"])
            if item_path.startswith(norm_dir):
                sum_severity += item.get("severity", 0.0)

        debt_penalty = min(sum_severity * 0.1, 1.0)

        shi = 1.0 * cohesion - 0.5 * coupling - 0.2 * debt_penalty
        return max(0.0, min(1.0, round(shi, 3)))

    def propose_refactoring(self, directory: str) -> str | None:
        """Generates proactive refactoring suggestions in markdown when SHI < 0.60."""
        shi = self.calculate_shi(directory)
        if shi >= 0.60:
            return None

        proposal = f"# Proactive Refactoring Proposal: `{os.path.basename(directory)}`\n\n"
        proposal += f"The subsystem health index (**SHI: {shi:.2f}**) has fallen below the acceptable threshold (0.60).\n"
        proposal += "This module is currently classified as an **Architectural Sink** and requires immediate refactoring.\n\n"

        proposal += "## Diagnostic Summary\n"
        coupling = self.calculate_coupling_ratio(directory)
        proposal += f"- **Coupling Ratio**: {coupling:.2f} (Target: < 0.3)\n"

        # Cohesion (LCOM) diagnostic
        lcom_scores = []
        for root, _, files in os.walk(directory):
            for file in files:
                if file.endswith(".py"):
                    file_path = os.path.join(root, file)
                    try:
                        with open(file_path, encoding="utf-8", errors="ignore") as f:
                            scores = self.calculate_lcom(f.read())
                            for cls, score in scores.items():
                                lcom_scores.append((cls, score, file))
                    except Exception:
                        pass
        if lcom_scores:
            avg_lcom = sum(s[1] for s in lcom_scores) / len(lcom_scores)
            cohesion = 1.0 - min(avg_lcom / 10.0, 1.0)
            proposal += f"- **Cohesion Score**: {cohesion:.2f} (Average LCOM: {avg_lcom:.1f})\n"
            high_lcom = [s for s in lcom_scores if s[1] > 5]
            if high_lcom:
                proposal += "\n### High Lack of Cohesion of Methods (LCOM) Classes:\n"
                for cls, score, file in high_lcom:
                    proposal += f"  - Class `{cls}` in `{file}` has LCOM score of {score}.\n"
        else:
            proposal += "- **Cohesion Score**: 1.00 (No classes found)\n"

        # Debt diagnostics
        norm_dir = os.path.abspath(directory)
        debt_items = []
        for item in self.ledger.get_items():
            item_path = os.path.abspath(item["file_path"])
            if item_path.startswith(norm_dir):
                debt_items.append(item)
        if debt_items:
            proposal += "\n## Outstanding Technical Debt Items\n"
            for item in debt_items:
                proposal += f"- **[{item['category'].upper()}]** `{os.path.basename(item['file_path'])}`: {item['description']} (Severity: {item['severity']})\n"

        proposal += "\n## Action Plan\n"
        proposal += "1. **Split High LCOM Classes**: Refactor classes with high cohesion issues into smaller, more focused modules.\n"
        proposal += "2. **Reduce Module Coupling**: Minimize external dependencies and enforce clean interface boundaries.\n"
        proposal += "3. **Clear Technical Debt**: Address the boundary layering violations and resolve critical warnings immediately.\n"
        return proposal

    def verify_boundaries(
        self, proposed_code: str, file_path: str, raise_on_violation: bool = False
    ) -> list[str]:
        """
        Checks proposed Python code for clean architecture layering and modular boundary violations.
        Returns a list of violations found.
        Raises ArchitectureDriftAlarm if raise_on_violation is True and violations exist.
        """
        violations = []
        # Standardize path slashes for matching
        norm_path = file_path.replace("\\", "/")

        # Identify if target file has active layering rules
        disallowed_prefixes: list[str] = []
        for prefix, rules in self.layering_rules:
            if norm_path.startswith(prefix):
                disallowed_prefixes.extend(rules)

        if not disallowed_prefixes:
            return violations

        try:
            tree = ast.parse(proposed_code)
        except SyntaxError:
            return violations

        for node in ast.walk(tree):
            imported_mod: str | None = None
            if isinstance(node, ast.Import):
                for name in node.names:
                    imported_mod = name.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_mod = node.module

            if imported_mod:
                norm_import = imported_mod.replace(".", "/")
                for disallowed in disallowed_prefixes:
                    if norm_import.startswith(disallowed):
                        violations.append(
                            f"Layering violation: '{file_path}' imports '{imported_mod}' which is banned."
                        )

        if raise_on_violation and violations:
            raise ArchitectureDriftAlarm("\n".join(violations))

        return violations

    def audit_architecture(self, file_path: str, code: str) -> dict[str, Any]:
        """
        Performs full cohesion and architectural boundary audit.
        Records violations and high LCOM classes in the Technical Debt Ledger.
        """
        self.ledger.clear_file_debt(file_path)

        lcom_scores = self.calculate_lcom(code)
        boundary_violations = self.verify_boundaries(code, file_path)

        # Log LCOM high scores as debt (LCOM > 5 is considered high)
        for class_name, score in lcom_scores.items():
            if score > 5:
                self.ledger.add_debt_item(
                    file_path=file_path,
                    category="cohesion",
                    description=f"Class '{class_name}' has high LCOM score ({score}). Consider splitting its responsibilities.",
                    severity=round(score * 0.1, 2),
                )

        # Log boundary violations as high-severity debt
        for violation in boundary_violations:
            self.ledger.add_debt_item(
                file_path=file_path,
                category="layering",
                description=violation,
                severity=1.5,
            )

        dir_path = os.path.dirname(os.path.abspath(file_path))
        coupling = self.calculate_coupling_ratio(dir_path)
        shi = self.calculate_shi(dir_path)
        proposal = self.propose_refactoring(dir_path)

        passed = len(boundary_violations) == 0
        return {
            "passed": passed,
            "lcom_scores": lcom_scores,
            "violations": boundary_violations,
            "total_debt": len(boundary_violations) + sum(1 for s in lcom_scores.values() if s > 5),
            "coupling_ratio": coupling,
            "shi": shi,
            "refactoring_proposal": proposal,
        }
