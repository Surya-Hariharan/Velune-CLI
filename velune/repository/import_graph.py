"""Import graph builder for Python, JavaScript, and TypeScript files.

Constructs a directed graph where an edge A → B means "A imports from B".
Computes structural metrics: fan_in (who imports this?), fan_out (what does this import?).

Phase 2a uses simple regex for JS/TS; full tree-sitter parsing in Phase 2b-1.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("velune.repository.import_graph")


@dataclass
class ImportMetrics:
    """Metrics for a module/symbol in the import graph."""

    module_path: str
    fan_in: int = 0  # Count of modules that import this module
    fan_out: int = 0  # Count of modules this module imports
    is_imported_by_tests: bool = False
    importers: set[str] = field(default_factory=set)  # Modules that import this
    imports: set[str] = field(default_factory=set)  # Modules this imports


class ImportGraphBuilder:
    """Builds an import dependency graph for a codebase.

    Extracts import statements from Python, JavaScript, and TypeScript files,
    then computes fan_in/fan_out metrics for each module.
    """

    def __init__(self) -> None:
        """Initialize the import graph builder."""
        self._graph: dict[str, ImportMetrics] = {}
        self._file_extensions = {".py", ".js", ".ts", ".jsx", ".tsx"}

    def build_from_directory(self, root_path: Path) -> dict[str, ImportMetrics]:
        """Scan directory recursively and build the import graph.

        Parameters
        ----------
        root_path:
            Root directory to scan for Python/JS/TS files.

        Returns
        -------
        dict[str, ImportMetrics]:
            Graph mapping module paths to their metrics.
        """
        self._graph.clear()

        # First pass: discover all files
        file_paths = self._discover_files(root_path)
        logger.info("Discovered %d source files", len(file_paths))

        # Second pass: extract imports from each file
        for file_path in file_paths:
            self._extract_imports(file_path, root_path)

        # Third pass: compute metrics
        self._compute_metrics()

        logger.info("Built import graph with %d modules", len(self._graph))
        return self._graph

    def _discover_files(self, root_path: Path) -> list[Path]:
        """Recursively discover all Python/JS/TS files."""
        files = []
        try:
            for path in root_path.rglob("*"):
                if path.is_file() and path.suffix in self._file_extensions:
                    # Skip common exclusions
                    if any(
                        part in ("node_modules", ".git", "__pycache__", ".venv", "venv")
                        for part in path.parts
                    ):
                        continue
                    files.append(path)
        except Exception as exc:
            logger.warning("Error discovering files in %s: %s", root_path, exc)
        return sorted(files)

    def _extract_imports(self, file_path: Path, root_path: Path) -> None:
        """Extract import statements from a single file."""
        try:
            relative_path = file_path.relative_to(root_path).as_posix()

            # Ensure module is in graph
            if relative_path not in self._graph:
                self._graph[relative_path] = ImportMetrics(module_path=relative_path)

            if file_path.suffix == ".py":
                self._extract_python_imports(file_path, relative_path)
            elif file_path.suffix in {".js", ".ts", ".jsx", ".tsx"}:
                self._extract_js_imports(file_path, relative_path)

        except Exception as exc:
            logger.debug("Failed to extract imports from %s: %s", file_path, exc)

    def _extract_python_imports(self, file_path: Path, relative_path: str) -> None:
        """Extract imports from a Python file using ast.parse()."""
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(content)

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        module_name = alias.name.split(".")[0]
                        self._add_import_edge(relative_path, module_name)

                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        module_name = node.module.split(".")[0]
                        self._add_import_edge(relative_path, module_name)

        except SyntaxError:
            logger.debug("Syntax error parsing %s", file_path)
        except Exception as exc:
            logger.debug("Error extracting imports from %s: %s", file_path, exc)

    def _extract_js_imports(self, file_path: Path, relative_path: str) -> None:
        """Extract imports from JS/TS files using regex (Phase 2a).

        Phase 2b will use full tree-sitter parsing for better accuracy.
        """
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")

            # ES6 import: import ... from 'module' or "module"
            es6_pattern = r"import\s+(?:{[^}]*}|[^'\"]*)\s+from\s+['\"]([^'\"]+)['\"]"
            for match in re.finditer(es6_pattern, content):
                module = match.group(1)
                # Normalize: ./foo → foo, ../foo → foo
                module = module.lstrip("./").split("/")[0]
                if module and not module.startswith("."):
                    self._add_import_edge(relative_path, module)

            # CommonJS require: require('module') or require("module")
            require_pattern = r"require\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"
            for match in re.finditer(require_pattern, content):
                module = match.group(1)
                module = module.lstrip("./").split("/")[0]
                if module and not module.startswith("."):
                    self._add_import_edge(relative_path, module)

        except Exception as exc:
            logger.debug("Error extracting imports from %s: %s", file_path, exc)

    def _add_import_edge(self, from_module: str, to_module: str) -> None:
        """Add an import edge A → B (A imports from B)."""
        if from_module == to_module:
            return  # Skip self-imports

        # Ensure both modules are in graph
        if from_module not in self._graph:
            self._graph[from_module] = ImportMetrics(module_path=from_module)
        if to_module not in self._graph:
            self._graph[to_module] = ImportMetrics(module_path=to_module)

        # Add edge
        self._graph[from_module].imports.add(to_module)
        self._graph[to_module].importers.add(from_module)

    def _compute_metrics(self) -> None:
        """Compute fan_in, fan_out, and test coverage metrics."""
        for metrics in self._graph.values():
            metrics.fan_in = len(metrics.importers)
            metrics.fan_out = len(metrics.imports)

            # Check if imported by any test file
            metrics.is_imported_by_tests = any(
                "test_" in importer or ".test." in importer for importer in metrics.importers
            )

    def get_metrics(self, module_path: str) -> ImportMetrics | None:
        """Get metrics for a specific module.

        Parameters
        ----------
        module_path:
            The relative module path.

        Returns
        -------
        ImportMetrics | None:
            Metrics if module exists, None otherwise.
        """
        return self._graph.get(module_path)

    def get_all_metrics(self) -> dict[str, ImportMetrics]:
        """Return the complete import graph."""
        return self._graph.copy()

    def get_importers(self, module_path: str) -> set[str]:
        """Get all modules that import the given module.

        Parameters
        ----------
        module_path:
            The module to query.

        Returns
        -------
        set[str]:
            Modules that import the given module.
        """
        metrics = self._graph.get(module_path)
        return metrics.importers if metrics else set()

    def get_imports(self, module_path: str) -> set[str]:
        """Get all modules imported by the given module.

        Parameters
        ----------
        module_path:
            The module to query.

        Returns
        -------
        set[str]:
            Modules imported by the given module.
        """
        metrics = self._graph.get(module_path)
        return metrics.imports if metrics else set()

    def get_transitive_dependents(self, module_path: str, max_depth: int = 5) -> set[str]:
        """Get all modules that depend on this module (transitively).

        Parameters
        ----------
        module_path:
            The module to query.
        max_depth:
            Maximum traversal depth to avoid cycles.

        Returns
        -------
        set[str]:
            All modules that transitively depend on this module.
        """
        dependents = set()
        queue = [(module_path, 0)]

        while queue:
            current, depth = queue.pop(0)
            if depth > max_depth:
                continue

            importers = self.get_importers(current)
            for importer in importers:
                if importer not in dependents:
                    dependents.add(importer)
                    queue.append((importer, depth + 1))

        return dependents
