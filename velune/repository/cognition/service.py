"""Repository cognition pipeline."""

from __future__ import annotations

import ast
import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Iterable

import networkx as nx

from velune.repository.cognition.schemas import (
    RepositoryEdge,
    RepositoryFile,
    RepositoryLanguage,
    RepositorySnapshot,
    RepositorySymbol,
    RepositorySymbolKind,
)


class RepositoryCognitionService:
    """Indexes repositories into a semantic graph."""

    code_extensions = {
        ".py": RepositoryLanguage.PYTHON,
        ".js": RepositoryLanguage.JAVASCRIPT,
        ".jsx": RepositoryLanguage.JAVASCRIPT,
        ".ts": RepositoryLanguage.TYPESCRIPT,
        ".tsx": RepositoryLanguage.TYPESCRIPT,
        ".go": RepositoryLanguage.GO,
        ".rs": RepositoryLanguage.RUST,
    }

    def __init__(self) -> None:
        self.graph = nx.MultiDiGraph()

    def index(self, root_path: Path, include_ignored: bool = False) -> RepositorySnapshot:
        files: list[RepositoryFile] = []
        symbols: list[RepositorySymbol] = []
        edges: list[RepositoryEdge] = []

        for file_path in self._iter_source_files(root_path, include_ignored=include_ignored):
            file_record = self._build_file_record(file_path)
            files.append(file_record)
            self.graph.add_node(file_record.path, kind="file", language=file_record.language.value, size_bytes=file_record.size_bytes)

            content = self._read_text(file_path)
            extracted_symbols, extracted_edges = self._extract_symbols(file_record, content)
            symbols.extend(extracted_symbols)
            edges.extend(extracted_edges)

            for symbol in extracted_symbols:
                self.graph.add_node(
                    symbol.name,
                    kind=symbol.kind.value,
                    file_path=symbol.file_path,
                    line_start=symbol.line_start,
                    line_end=symbol.line_end,
                )
                self.graph.add_edge(file_record.path, symbol.name, edge_type="contains")

            for edge in extracted_edges:
                self.graph.add_edge(edge.source, edge.target, edge_type=edge.edge_type, weight=edge.weight)

        summary = self._build_summary(files, symbols, edges)
        return RepositorySnapshot(root_path=str(root_path), files=files, symbols=symbols, edges=edges, summary=summary)

    def traverse(self, node_id: str, depth: int = 2) -> list[str]:
        """Traverse repository relationships from a node."""

        if node_id not in self.graph:
            return []

        seen: set[str] = {node_id}
        frontier = {node_id}
        for _ in range(depth):
            next_frontier: set[str] = set()
            for current in frontier:
                next_frontier.update(self.graph.successors(current))
                next_frontier.update(self.graph.predecessors(current))
            frontier = next_frontier - seen
            seen.update(frontier)
        return list(seen)

    def _iter_source_files(self, root_path: Path, include_ignored: bool) -> Iterable[Path]:
        for file_path in root_path.rglob("*"):
            if not file_path.is_file():
                continue
            if not include_ignored and any(part in {".git", "node_modules", "dist", "build", "__pycache__"} for part in file_path.parts):
                continue
            if file_path.suffix.lower() in self.code_extensions:
                yield file_path

    def _build_file_record(self, file_path: Path) -> RepositoryFile:
        return RepositoryFile(
            path=str(file_path),
            language=self.code_extensions.get(file_path.suffix.lower(), RepositoryLanguage.UNKNOWN),
            size_bytes=file_path.stat().st_size,
            sha256=self._sha256(file_path),
        )

    def _extract_symbols(self, file_record: RepositoryFile, content: str) -> tuple[list[RepositorySymbol], list[RepositoryEdge]]:
        if file_record.language == RepositoryLanguage.PYTHON:
            return self._extract_python(content, file_record.path)
        return self._extract_regex(content, file_record.path, file_record.language)

    def _extract_python(self, content: str, file_path: str) -> tuple[list[RepositorySymbol], list[RepositoryEdge]]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return [], []

        symbols: list[RepositorySymbol] = []
        edges: list[RepositoryEdge] = []

        class PythonSymbolVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.class_stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                symbols.append(
                    RepositorySymbol(
                        name=node.name,
                        kind=RepositorySymbolKind.CLASS,
                        file_path=file_path,
                        line_start=getattr(node, "lineno", 0),
                        line_end=getattr(node, "end_lineno", getattr(node, "lineno", 0)),
                    )
                )
                self.class_stack.append(node.name)
                self.generic_visit(node)
                self.class_stack.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

            def visit_Import(self, node: ast.Import) -> None:
                self._visit_import(node.names, getattr(node, "lineno", 0), getattr(node, "end_lineno", getattr(node, "lineno", 0)), None)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                self._visit_import(node.names, getattr(node, "lineno", 0), getattr(node, "end_lineno", getattr(node, "lineno", 0)), getattr(node, "module", None))

            def _visit_function(self, node: ast.AST) -> None:
                assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                kind = RepositorySymbolKind.METHOD if self.class_stack else RepositorySymbolKind.FUNCTION
                symbols.append(
                    RepositorySymbol(
                        name=node.name,
                        kind=kind,
                        file_path=file_path,
                        line_start=getattr(node, "lineno", 0),
                        line_end=getattr(node, "end_lineno", getattr(node, "lineno", 0)),
                        parent=self.class_stack[-1] if self.class_stack else None,
                    )
                )
                self.generic_visit(node)

            def _visit_import(self, names: list[ast.alias], line_start: int, line_end: int, module: str | None) -> None:
                for alias in names:
                    target = alias.name
                    edges.append(RepositoryEdge(source=file_path, target=target, edge_type="imports"))
                    symbols.append(
                        RepositorySymbol(
                            name=target,
                            kind=RepositorySymbolKind.IMPORT,
                            file_path=file_path,
                            line_start=line_start,
                            line_end=line_end,
                            metadata={"module": module},
                        )
                    )

        PythonSymbolVisitor().visit(tree)

        return symbols, edges

    def _extract_regex(self, content: str, file_path: str, language: RepositoryLanguage) -> tuple[list[RepositorySymbol], list[RepositoryEdge]]:
        patterns = {
            RepositoryLanguage.JAVASCRIPT: [r"(?:export\s+)?function\s+(\w+)", r"class\s+(\w+)", r"import\s+.*?from\s+['\"]([^'\"]+)['\"]"],
            RepositoryLanguage.TYPESCRIPT: [r"(?:export\s+)?function\s+(\w+)", r"class\s+(\w+)", r"import\s+.*?from\s+['\"]([^'\"]+)['\"]"],
            RepositoryLanguage.GO: [r"func\s+(\w+)", r"type\s+(\w+)\s+struct", r"import\s+\((.*?)\)"],
            RepositoryLanguage.RUST: [r"(?:pub\s+)?fn\s+(\w+)", r"(?:pub\s+)?struct\s+(\w+)", r"use\s+([^;]+);"],
        }

        symbols: list[RepositorySymbol] = []
        edges: list[RepositoryEdge] = []

        for pattern in patterns.get(language, []):
            for match in re.finditer(pattern, content, flags=re.MULTILINE | re.DOTALL):
                value = match.group(1).strip()
                kind = RepositorySymbolKind.FUNCTION
                if "class" in pattern or "struct" in pattern or language == RepositoryLanguage.GO and "type" in pattern:
                    kind = RepositorySymbolKind.CLASS
                if "import" in pattern or "use" in pattern:
                    kind = RepositorySymbolKind.IMPORT
                    edges.append(RepositoryEdge(source=file_path, target=value, edge_type="imports"))
                symbols.append(
                    RepositorySymbol(
                        name=value,
                        kind=kind,
                        file_path=file_path,
                        metadata={"language": language.value},
                    )
                )

        return symbols, edges

    def _sha256(self, file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(8192), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _read_text(self, file_path: Path) -> str:
        try:
            return file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return file_path.read_text(encoding="latin-1", errors="ignore")

    def _build_summary(self, files: list[RepositoryFile], symbols: list[RepositorySymbol], edges: list[RepositoryEdge]) -> dict[str, object]:
        by_language = defaultdict(int)
        for file_record in files:
            by_language[file_record.language.value] += 1

        return {
            "files": len(files),
            "symbols": len(symbols),
            "edges": len(edges),
            "languages": dict(by_language),
        }