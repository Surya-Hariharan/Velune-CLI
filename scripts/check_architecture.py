#!/usr/bin/env python3
"""Architecture linting rules to prevent layer violations."""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LayerRule:
    """Defines a forbidden import relationship between layers."""
    source_pattern: str  # e.g., "velune/kernel/*"
    forbidden_import: str  # e.g., "velune/cli"
    reason: str


# Define layer boundary rules
LAYER_RULES = [
    LayerRule(
        source_pattern="velune/kernel",
        forbidden_import="velune/cli",
        reason="Kernel (OS layer) must not depend on CLI (UI layer)"
    ),
    LayerRule(
        source_pattern="velune/kernel",
        forbidden_import="velune/cognition",
        reason="Kernel (infrastructure) must not depend on cognition (application)"
    ),
    LayerRule(
        source_pattern="velune/providers",
        forbidden_import="velune/cognition",
        reason="Providers (infrastructure) must not depend on cognition (application logic)"
    ),
    LayerRule(
        source_pattern="velune/providers",
        forbidden_import="velune/cli",
        reason="Providers (infrastructure) must not depend on CLI (user interface)"
    ),
    LayerRule(
        source_pattern="velune/memory",
        forbidden_import="velune/cli",
        reason="Memory (infrastructure) must not depend on CLI (user interface)"
    ),
    LayerRule(
        source_pattern="velune/memory",
        forbidden_import="velune/cognition",
        reason="Memory (infrastructure) must not depend on cognition (application logic)"
    ),
    LayerRule(
        source_pattern="velune/retrieval",
        forbidden_import="velune/cli",
        reason="Retrieval (infrastructure) must not depend on CLI (user interface)"
    ),
    LayerRule(
        source_pattern="velune/telemetry",
        forbidden_import="velune/cognition",
        reason="Telemetry (infrastructure) must not depend on cognition (application logic)"
    ),
]


def get_module_imports(file_path: Path) -> set[str]:
    """Extract all import names from a Python file using AST."""
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=str(file_path))
    except (SyntaxError, UnicodeDecodeError) as e:
        print(f"  Warning: Could not parse {file_path}: {e}")
        return set()

    imports = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)

    return imports


def file_matches_pattern(file_path: Path, pattern: str) -> bool:
    """Check if a file path matches a pattern (e.g., 'velune/kernel/*')."""
    # Convert to forward slashes for consistent matching
    file_str = str(file_path).replace("\\", "/")
    pattern_str = pattern.replace("\\", "/")

    # Handle wildcard patterns
    if "*" in pattern_str:
        # Remove the wildcard and check if file starts with the pattern prefix
        prefix = pattern_str.replace("*", "")
        return file_str.startswith(prefix)
    else:
        # Exact directory match
        return file_str.startswith(pattern_str)


def check_architecture() -> int:
    """Check architecture rules. Returns 0 if all pass, 1 if violations found."""
    violations = []
    velune_dir = Path("velune")

    if not velune_dir.exists():
        print("Error: velune/ directory not found")
        return 1

    # Find all Python files
    python_files = list(velune_dir.rglob("*.py"))

    print(f"Checking {len(python_files)} Python files for architecture violations...\n")

    for file_path in sorted(python_files):
        # Skip __pycache__
        if "__pycache__" in str(file_path):
            continue

        # Check each rule for this file
        for rule in LAYER_RULES:
            if not file_matches_pattern(file_path, rule.source_pattern):
                continue

            # This file matches the source pattern, check for forbidden imports
            imports = get_module_imports(file_path)

            for imp in imports:
                if imp.startswith(rule.forbidden_import):
                    violations.append({
                        "file": str(file_path).replace("\\", "/"),
                        "import": imp,
                        "source_pattern": rule.source_pattern,
                        "forbidden_pattern": rule.forbidden_import,
                        "reason": rule.reason,
                    })

    if violations:
        print("❌ ARCHITECTURE VIOLATIONS FOUND:\n")
        for v in violations:
            print(f"  {v['file']}")
            print(f"    ↳ imports {v['import']}")
            print(f"    ↳ Rule: {v['reason']}")
            print()
        return 1

    print("✓ All architecture rules pass")
    return 0


if __name__ == "__main__":
    sys.exit(check_architecture())
