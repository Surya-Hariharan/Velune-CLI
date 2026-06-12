"""Tree-sitter and AST multi-language parser with regex fallbacks.

This module provides :class:`RepositorySnapshotParser` — the synchronous
parser that returns :class:`~velune.repository.schemas.RepositorySymbol` /
:class:`~velune.repository.schemas.RepositoryEdge` objects for use by the
indexer, incremental indexer, and tools.  It is intentionally distinct from
:class:`~velune.repository.ast_parser.ASTParser`, which is the async parser
used by :class:`~velune.repository.symbol_registry.SymbolRegistry` and
:class:`~velune.repository.rename_journal.RenameJournal`.

The lazy tree-sitter import pattern is preserved here: tree-sitter DLLs are
not loaded until the first actual ``parse()`` call, avoiding Windows Defender
real-time-scan startup costs.

.. deprecated alias:
   ``ASTParser`` is kept as a backwards-compatible alias for
   ``RepositorySnapshotParser`` so existing callers continue to work while
   they are updated to use the canonical name.
"""

import ast
import re
from pathlib import Path
from typing import Any

from velune.repository.schemas import (
    RepositoryEdge,
    RepositoryLanguage,
    RepositorySymbol,
    RepositorySymbolKind,
)

# Tree-sitter ships compiled C extensions (.pyd/.so). Importing them at module
# load time forces Windows to load several DLLs synchronously — each triggering
# a Defender real-time scan — adding seconds to *every* startup, even when no
# parsing is requested. We therefore defer all tree-sitter imports until the
# first actual parse via ``_ensure_tree_sitter``.
#
# ``HAS_TREE_SITTER`` starts ``None`` (unknown) and becomes ``True``/``False``
# after the first lazy load attempt. It remains importable for callers/tests
# that reference it, but no import cost is paid until parsing happens.
HAS_TREE_SITTER: bool | None = None
_TS_LANGUAGES: dict[str, Any] = {}
_TS_PARSER_CLS: Any = None


def _ensure_tree_sitter() -> bool:
    """Lazily import tree-sitter grammars on first use. Returns availability."""
    global HAS_TREE_SITTER, _TS_PARSER_CLS
    if HAS_TREE_SITTER is not None:
        return HAS_TREE_SITTER
    try:
        import tree_sitter_go
        import tree_sitter_python
        import tree_sitter_rust
        import tree_sitter_typescript
        from tree_sitter import Language, Parser

        _TS_PARSER_CLS = Parser
        for name, factory in (
            ("python", tree_sitter_python.language),
            ("typescript", tree_sitter_typescript.language_typescript),
            ("javascript", tree_sitter_typescript.language_typescript),
            ("go", tree_sitter_go.language),
            ("rust", tree_sitter_rust.language),
        ):
            try:
                _TS_LANGUAGES[name] = Language(factory())
            except Exception:
                pass
        HAS_TREE_SITTER = True
    except ImportError:
        HAS_TREE_SITTER = False
    return HAS_TREE_SITTER


class RepositorySnapshotParser:
    """Multi-language AST and symbol parser with comprehensive fallbacks.

    Returns :class:`~velune.repository.schemas.RepositorySymbol` /
    :class:`~velune.repository.schemas.RepositoryEdge` pairs suitable for
    building a :class:`~velune.repository.schemas.RepositorySnapshot`.

    Uses tree-sitter when available (loaded lazily to avoid DLL startup cost),
    falls back to Python's built-in ``ast`` module for Python files, and
    finally falls back to regex for other languages.
    """

    def __init__(self) -> None:
        # Languages are populated lazily on first parse — see _ensure_loaded.
        self.languages: dict[str, Any] = {}
        self._parsers: dict[str, Any] = {}
        self._loaded = False

    def _ensure_loaded(self) -> bool:
        """Populate ``self.languages`` from the lazily-loaded grammar cache."""
        if self._loaded:
            return bool(self.languages)
        self._loaded = True
        if _ensure_tree_sitter():
            self.languages = dict(_TS_LANGUAGES)
        return bool(self.languages)

    def parse(
        self, file_path: Path, code: str
    ) -> tuple[list[RepositorySymbol], list[RepositoryEdge]]:
        """Parses source code from file_path, leveraging tree-sitter or fallbacks."""
        lang = self._detect_language(file_path)

        # Try tree-sitter if available (loaded lazily on first parse)
        if self._ensure_loaded() and lang.value in self.languages:
            try:
                return self._parse_tree_sitter(file_path, code, lang)
            except Exception:
                # Fail silently and let fallbacks handle it
                pass

        # Fallbacks
        if lang == RepositoryLanguage.PYTHON:
            return self._parse_python_ast(file_path, code)

        return self._parse_regex(file_path, code, lang)

    def _detect_language(self, file_path: Path) -> RepositoryLanguage:
        """Detect language from file path extension."""
        suffix = file_path.suffix.lower()
        mapping = {
            ".py": RepositoryLanguage.PYTHON,
            ".js": RepositoryLanguage.JAVASCRIPT,
            ".jsx": RepositoryLanguage.JAVASCRIPT,
            ".ts": RepositoryLanguage.TYPESCRIPT,
            ".tsx": RepositoryLanguage.TYPESCRIPT,
            ".go": RepositoryLanguage.GO,
            ".rs": RepositoryLanguage.RUST,
        }
        return mapping.get(suffix, RepositoryLanguage.UNKNOWN)

    def _parse_tree_sitter(
        self, file_path: Path, code: str, lang: RepositoryLanguage
    ) -> tuple[list[RepositorySymbol], list[RepositoryEdge]]:
        """Uses tree-sitter to parse the code and extract symbols and imports."""
        if lang.value not in self._parsers:
            self._parsers[lang.value] = _TS_PARSER_CLS(self.languages[lang.value])
        parser = self._parsers[lang.value]
        tree = parser.parse(bytes(code, "utf8"))

        symbols: list[RepositorySymbol] = []
        edges: list[RepositoryEdge] = []
        file_path_str = str(file_path)

        def walk(node: Any, parent_class: str | None = None) -> None:
            node_type = node.type
            name = ""
            kind = RepositorySymbolKind.UNKNOWN
            current_class = parent_class

            # Python types
            if lang == RepositoryLanguage.PYTHON:
                if node_type == "class_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        name = code[name_node.start_byte : name_node.end_byte]
                        kind = RepositorySymbolKind.CLASS
                        current_class = name
                elif node_type == "function_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        name = code[name_node.start_byte : name_node.end_byte]
                        kind = (
                            RepositorySymbolKind.METHOD
                            if parent_class
                            else RepositorySymbolKind.FUNCTION
                        )
                elif node_type in ("import_statement", "import_from_statement"):
                    text = code[node.start_byte : node.end_byte]
                    for match in re.finditer(r"(?:import|from)\s+([\w.]+)", text):
                        target = match.group(1)
                        symbols.append(
                            RepositorySymbol(
                                name=target,
                                kind=RepositorySymbolKind.IMPORT,
                                file_path=file_path_str,
                                line_start=node.start_point[0] + 1,
                                line_end=node.end_point[0] + 1,
                            )
                        )
                        edges.append(
                            RepositoryEdge(source=file_path_str, target=target, edge_type="imports")
                        )

            # JS/TS types
            elif lang in (RepositoryLanguage.JAVASCRIPT, RepositoryLanguage.TYPESCRIPT):
                if node_type == "class_declaration":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        name = code[name_node.start_byte : name_node.end_byte]
                        kind = RepositorySymbolKind.CLASS
                        current_class = name
                elif node_type in ("function_declaration", "method_definition"):
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        name = code[name_node.start_byte : name_node.end_byte]
                        kind = (
                            RepositorySymbolKind.METHOD
                            if parent_class
                            else RepositorySymbolKind.FUNCTION
                        )
                elif node_type == "import_statement":
                    text = code[node.start_byte : node.end_byte]
                    match = re.search(r"from\s+['\"]([^'\"]+)['\"]", text)
                    if match:
                        target = match.group(1)
                        symbols.append(
                            RepositorySymbol(
                                name=target,
                                kind=RepositorySymbolKind.IMPORT,
                                file_path=file_path_str,
                                line_start=node.start_point[0] + 1,
                                line_end=node.end_point[0] + 1,
                            )
                        )
                        edges.append(
                            RepositoryEdge(source=file_path_str, target=target, edge_type="imports")
                        )

            # Go types
            elif lang == RepositoryLanguage.GO:
                if node_type == "type_declaration":
                    text = code[node.start_byte : node.end_byte]
                    match = re.search(r"type\s+(\w+)\s+(?:struct|interface)", text)
                    if match:
                        name = match.group(1)
                        kind = RepositorySymbolKind.CLASS
                        current_class = name
                elif node_type == "function_declaration":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        name = code[name_node.start_byte : name_node.end_byte]
                        kind = RepositorySymbolKind.FUNCTION
                elif node_type == "method_declaration":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        name = code[name_node.start_byte : name_node.end_byte]
                        kind = RepositorySymbolKind.METHOD
                elif node_type == "import_spec":
                    text = code[node.start_byte : node.end_byte]
                    match = re.search(r"['\"]([^'\"]+)['\"]", text)
                    if match:
                        target = match.group(1)
                        symbols.append(
                            RepositorySymbol(
                                name=target,
                                kind=RepositorySymbolKind.IMPORT,
                                file_path=file_path_str,
                                line_start=node.start_point[0] + 1,
                                line_end=node.end_point[0] + 1,
                            )
                        )
                        edges.append(
                            RepositoryEdge(source=file_path_str, target=target, edge_type="imports")
                        )

            # Rust types
            elif lang == RepositoryLanguage.RUST:
                if node_type in ("struct_item", "impl_item"):
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        name = code[name_node.start_byte : name_node.end_byte]
                        kind = RepositorySymbolKind.CLASS
                        current_class = name
                elif node_type == "function_item":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        name = code[name_node.start_byte : name_node.end_byte]
                        kind = (
                            RepositorySymbolKind.METHOD
                            if parent_class
                            else RepositorySymbolKind.FUNCTION
                        )
                elif node_type == "use_declaration":
                    text = code[node.start_byte : node.end_byte]
                    match = re.search(r"use\s+([^;]+);", text)
                    if match:
                        target = match.group(1).strip()
                        symbols.append(
                            RepositorySymbol(
                                name=target,
                                kind=RepositorySymbolKind.IMPORT,
                                file_path=file_path_str,
                                line_start=node.start_point[0] + 1,
                                line_end=node.end_point[0] + 1,
                            )
                        )
                        edges.append(
                            RepositoryEdge(source=file_path_str, target=target, edge_type="imports")
                        )

            # Append structured symbol if matched
            if name and kind != RepositorySymbolKind.UNKNOWN:
                symbols.append(
                    RepositorySymbol(
                        name=name,
                        kind=kind,
                        file_path=file_path_str,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        parent=parent_class,
                    )
                )

            # Recurse children
            for child in node.children:
                walk(child, current_class)

        walk(tree.root_node)
        return symbols, edges

    def _parse_python_ast(
        self, file_path: Path, code: str
    ) -> tuple[list[RepositorySymbol], list[RepositoryEdge]]:
        """Standard Python AST library fallback."""
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return [], []

        symbols: list[RepositorySymbol] = []
        edges: list[RepositoryEdge] = []
        file_path_str = str(file_path)

        class PythonVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.class_stack: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                doc = ast.get_docstring(node)
                symbols.append(
                    RepositorySymbol(
                        name=node.name,
                        kind=RepositorySymbolKind.CLASS,
                        file_path=file_path_str,
                        line_start=getattr(node, "lineno", 1),
                        line_end=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                        docstring=doc,
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
                for alias in node.names:
                    target = alias.name
                    edges.append(
                        RepositoryEdge(source=file_path_str, target=target, edge_type="imports")
                    )
                    symbols.append(
                        RepositorySymbol(
                            name=target,
                            kind=RepositorySymbolKind.IMPORT,
                            file_path=file_path_str,
                            line_start=node.lineno,
                            line_end=getattr(node, "end_lineno", node.lineno),
                        )
                    )

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                mod = node.module or ""
                for alias in node.names:
                    target = f"{mod}.{alias.name}" if mod else alias.name
                    edges.append(
                        RepositoryEdge(source=file_path_str, target=target, edge_type="imports")
                    )
                    symbols.append(
                        RepositorySymbol(
                            name=alias.name,
                            kind=RepositorySymbolKind.IMPORT,
                            file_path=file_path_str,
                            line_start=node.lineno,
                            line_end=getattr(node, "end_lineno", node.lineno),
                            metadata={"module": mod},
                        )
                    )

            def _visit_function(self, node: Any) -> None:
                doc = ast.get_docstring(node)
                kind = (
                    RepositorySymbolKind.METHOD
                    if self.class_stack
                    else RepositorySymbolKind.FUNCTION
                )
                symbols.append(
                    RepositorySymbol(
                        name=node.name,
                        kind=kind,
                        file_path=file_path_str,
                        line_start=node.lineno,
                        line_end=getattr(node, "end_lineno", node.lineno),
                        docstring=doc,
                        parent=self.class_stack[-1] if self.class_stack else None,
                    )
                )
                self.generic_visit(node)

        PythonVisitor().visit(tree)
        return symbols, edges

    def _parse_regex(
        self, file_path: Path, code: str, lang: RepositoryLanguage
    ) -> tuple[list[RepositorySymbol], list[RepositoryEdge]]:
        """Universal regex symbol extractor fallback."""
        patterns = {
            RepositoryLanguage.JAVASCRIPT: [
                (r"(?:export\s+)?class\s+(\w+)", RepositorySymbolKind.CLASS),
                (r"(?:export\s+)?function\s+(\w+)", RepositorySymbolKind.FUNCTION),
                (r"import\s+.*?from\s+['\"]([^'\"]+)['\"]", RepositorySymbolKind.IMPORT),
            ],
            RepositoryLanguage.TYPESCRIPT: [
                (r"(?:export\s+)?class\s+(\w+)", RepositorySymbolKind.CLASS),
                (r"(?:export\s+)?(?:async\s+)?function\s+(\w+)", RepositorySymbolKind.FUNCTION),
                (r"import\s+.*?from\s+['\"]([^'\"]+)['\"]", RepositorySymbolKind.IMPORT),
            ],
            RepositoryLanguage.GO: [
                (r"type\s+(\w+)\s+struct", RepositorySymbolKind.CLASS),
                (r"func\s+(\w+)", RepositorySymbolKind.FUNCTION),
                (r"import\s+['\"]([^'\"]+)['\"]", RepositorySymbolKind.IMPORT),
            ],
            RepositoryLanguage.RUST: [
                (r"(?:pub\s+)?struct\s+(\w+)", RepositorySymbolKind.CLASS),
                (r"(?:pub\s+)?(?:async\s+)?fn\s+(\w+)", RepositorySymbolKind.FUNCTION),
                (r"use\s+([^;]+);", RepositorySymbolKind.IMPORT),
            ],
        }

        symbols: list[RepositorySymbol] = []
        edges: list[RepositoryEdge] = []
        file_path_str = str(file_path)

        for pattern, kind in patterns.get(lang, []):
            for match in re.finditer(pattern, code):
                value = match.group(1).strip()
                start_char = match.start()
                line_no = code[:start_char].count("\n") + 1

                if kind == RepositorySymbolKind.IMPORT:
                    edges.append(
                        RepositoryEdge(source=file_path_str, target=value, edge_type="imports")
                    )

                symbols.append(
                    RepositorySymbol(
                        name=value,
                        kind=kind,
                        file_path=file_path_str,
                        line_start=line_no,
                        line_end=line_no,
                    )
                )

        return symbols, edges


# ---------------------------------------------------------------------------
# Backwards-compatibility alias
# Callers that import ``from velune.repository.parser import ASTParser`` will
# continue to work while they migrate to ``RepositorySnapshotParser``.
# ---------------------------------------------------------------------------
ASTParser = RepositorySnapshotParser
