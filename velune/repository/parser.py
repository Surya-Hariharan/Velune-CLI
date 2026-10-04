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
import json
import re
from pathlib import Path
from typing import Any

from velune.repository.schemas import (
    EXTENSION_LANGUAGE_MAP,
    RepositoryEdge,
    RepositoryLanguage,
    RepositorySymbol,
    RepositorySymbolKind,
)


def _leading_decorator_texts(code: str, def_line_idx: int) -> list[str]:
    """Collect consecutive ``@decorator`` lines immediately above a def/class line.

    Text-based rather than tree-node-based: tree-sitter's Python grammar
    wraps a decorated definition in a separate ``decorated_definition``
    node, and depending on its exact sibling structure here isn't worth it
    when this module already falls back to raw-text regex extraction
    elsewhere inside the tree-sitter walk (e.g. import-statement handling).
    ``def_line_idx`` is 0-indexed (tree-sitter's ``node.start_point[0]``).
    """
    lines = code.splitlines()
    decorators: list[str] = []
    i = def_line_idx - 1
    while i >= 0:
        stripped = lines[i].strip()
        if not stripped.startswith("@"):
            break
        decorators.append(stripped[1:].split("(")[0].strip())
        i -= 1
    decorators.reverse()
    return decorators


def _dotted_names(nodes: list[ast.expr]) -> list[str]:
    """Extract dotted names from AST decorator/base-class expression lists.

    Handles ``@app.get(...)`` (a ``Call`` wrapping an ``Attribute``),
    ``@staticmethod`` (a bare ``Name``), and ``class Foo(pkg.Base)`` (an
    ``Attribute``) uniformly, returning e.g. ``"app.get"``/``"staticmethod"``/
    ``"pkg.Base"``. Anything else (a subscript, a lambda, ...) is skipped
    rather than guessed at.

    Feeds ``RepositorySymbol.metadata["decorators"/"bases"]`` — the
    structural evidence ``CodebaseAnalyzer``'s shape-based layer fallback
    (decorator-based routing, ORM model shape) reads instead of relying
    solely on folder-name conventions. See
    the repository-intelligence baseline audit §8/Recommendation on framework
    shape fingerprinting.
    """
    names: list[str] = []
    for node in nodes:
        target = node.func if isinstance(node, ast.Call) else node
        parts: list[str] = []
        while isinstance(target, ast.Attribute):
            parts.append(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            parts.append(target.id)
            names.append(".".join(reversed(parts)))
    return names


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
        if file_path.suffix.lower() == ".ipynb":
            code = self._extract_notebook_source(code)
            if not code:
                return [], []

        lang = self._detect_language(file_path)
        symbols: list[RepositorySymbol] | None = None
        edges: list[RepositoryEdge] | None = None

        # Try tree-sitter if available (loaded lazily on first parse)
        if self._ensure_loaded() and lang.value in self.languages:
            try:
                symbols, edges = self._parse_tree_sitter(file_path, code, lang)
            except Exception:
                # Fail silently and let fallbacks handle it
                symbols, edges = None, None

        if symbols is None or edges is None:
            # Fallbacks
            if lang == RepositoryLanguage.PYTHON:
                symbols, edges = self._parse_python_ast(file_path, code)
            else:
                symbols, edges = self._parse_regex(file_path, code, lang)

        # Dynamic-import detection is a regex-over-raw-text pass independent
        # of which backend produced the symbols above (tree-sitter's own
        # walker, like the AST/regex fallbacks, only ever recognized static
        # import statements — a call expression like
        # importlib.import_module(...) or require(...) was invisible to all
        # three, not just the fallback path). Running it here once, after
        # whichever backend ran, covers all of them uniformly.
        dyn_symbols, dyn_edges = self._extract_dynamic_imports(file_path, code, lang)
        if dyn_symbols:
            symbols = symbols + dyn_symbols
            edges = edges + dyn_edges

        return symbols, edges

    def parse_file(
        self, file_path: Path, code: str
    ) -> tuple[list[RepositorySymbol], list[RepositoryEdge]]:
        """Backward-compatible alias for :meth:`parse`.

        Older callers and some integration tests used ``parse_file`` before
        ``RepositorySnapshotParser`` settled on ``parse`` as the canonical API.
        Keep the alias additive so external integrations do not break.
        """
        return self.parse(file_path, code)

    def _detect_language(self, file_path: Path) -> RepositoryLanguage:
        """Detect language from file path extension.

        Sourced from ``schemas.EXTENSION_LANGUAGE_MAP`` — the same table
        ``FilesystemScanner`` uses to decide what to discover — so a file
        the scanner walks always gets a real language tag here rather than
        silently collapsing to UNKNOWN because this module's own mapping
        hadn't caught up (previously true for e.g. ``.cs``, ``.php``, ``.rb``,
        every one of which the scanner already discovered).
        """
        suffix = file_path.suffix.lower()
        return EXTENSION_LANGUAGE_MAP.get(suffix, RepositoryLanguage.UNKNOWN)

    # Dynamic-loading call shapes this can recognize, per language. This is
    # a regex-over-raw-text pass (see ``parse``) rather than an AST/tree-
    # sitter node match — deliberately, since it needs to run uniformly
    # after whichever backend produced the main symbol set.
    _DYNAMIC_IMPORT_CALL_PATTERNS: dict[RepositoryLanguage, str] = {
        RepositoryLanguage.PYTHON: (
            r"(?:importlib\.import_module|importlib\.__import__|__import__)\s*\(\s*([^)]*?)\s*\)"
        ),
        RepositoryLanguage.JAVASCRIPT: r"\brequire\s*\(\s*([^)]*?)\s*\)",
        RepositoryLanguage.TYPESCRIPT: r"\brequire\s*\(\s*([^)]*?)\s*\)",
    }

    def _extract_dynamic_imports(
        self, file_path: Path, code: str, lang: RepositoryLanguage
    ) -> tuple[list[RepositorySymbol], list[RepositoryEdge]]:
        """Detect dynamic-loading call sites (``importlib.import_module``,
        ``require(var)``) that every AST/tree-sitter/regex backend above
        anchors past, since they all only ever recognized static import
        *statements*, never these call *expressions* (baseline §6.7/§8:
        "Static-only import resolution with no dynamic-import awareness at
        all... a huge share of 'AI agent broke something it never saw'
        incidents come from" exactly this).

        Confidence-scored rather than silently dropped or fabricated as a
        certain edge:

        - A fully literal argument (``import_module("plugins.foo")``) is
          just as resolvable as a static import — full confidence, and the
          grapher treats it identically to one.
        - An f-string/template literal with a literal prefix before the
          first interpolation (``f"plugins.{name}"``) keeps that prefix as
          a lower-confidence target: the grapher expands it to every real
          file whose module/path starts with it, rather than one guessed
          file.
        - A fully dynamic argument (a bare variable, no literal information
          at all) can't name any target — recorded as a symbol only (this
          file dynamically loads *something* here), no edge, since a wrong
          edge is worse than an honestly absent one.
        """
        pattern = self._DYNAMIC_IMPORT_CALL_PATTERNS.get(lang)
        if pattern is None:
            return [], []

        symbols: list[RepositorySymbol] = []
        edges: list[RepositoryEdge] = []
        file_path_str = str(file_path)

        for match in re.finditer(pattern, code):
            arg = match.group(1).strip()
            line_no = code[: match.start()].count("\n") + 1
            target, confidence, is_dynamic = self._classify_dynamic_import_arg(arg)

            if target is None:
                symbols.append(
                    RepositorySymbol(
                        name="<dynamic import>",
                        kind=RepositorySymbolKind.IMPORT,
                        file_path=file_path_str,
                        line_start=line_no,
                        line_end=line_no,
                        metadata={"dynamic": True, "resolution_confidence": confidence},
                    )
                )
                continue

            symbols.append(
                RepositorySymbol(
                    name=target,
                    kind=RepositorySymbolKind.IMPORT,
                    file_path=file_path_str,
                    line_start=line_no,
                    line_end=line_no,
                    metadata=(
                        {"dynamic": True, "resolution_confidence": confidence} if is_dynamic else {}
                    ),
                )
            )
            edges.append(
                RepositoryEdge(
                    source=file_path_str,
                    target=target,
                    edge_type="imports_dynamic" if is_dynamic else "imports",
                    weight=confidence,
                )
            )

        return symbols, edges

    @staticmethod
    def _classify_dynamic_import_arg(arg: str) -> tuple[str | None, float, bool]:
        """Classify a dynamic-import call's raw argument text.

        Returns ``(target, confidence, is_dynamic)``:

        - A plain string literal → ``(literal_value, 1.0, False)`` — this
          is just as resolvable as any static import.
        - An f-string/template literal with a literal prefix before its
          first interpolation → ``(prefix, 0.5, True)``.
        - Anything else (bare identifier, expression, f-string with no
          literal prefix at all) → ``(None, 0.2, True)``.
        """
        m = re.fullmatch(r"['\"]([^'\"]*)['\"]", arg)
        if m:
            return m.group(1), 1.0, False

        m = re.match(r"f?[\"']([^\"'{]*)\{", arg) or re.match(r"`([^`$]*)\$\{", arg)
        if m:
            prefix = m.group(1).rstrip("./")
            return (prefix or None), 0.5, True

        return None, 0.2, True

    def _extract_notebook_source(self, raw_json: str) -> str:
        """Reconstruct synthetic Python source from a Jupyter notebook's code cells.

        Cells are concatenated in **execution-count order**, not cell
        position — the logical order the author actually ran, which is what
        the notebook's real symbol dependencies follow (a helper defined in
        a later cell but run first is meaningful; document order is not).
        Cells never executed (``execution_count`` is ``None``, e.g. after a
        "restart and clear outputs" before commit) are appended afterward in
        their original position order, since there's no run-order signal for
        them at all.

        A non-Python kernel or malformed/non-notebook JSON degrades to an
        empty string (zero symbols), never a raised exception — parsing a
        notebook is inherently best-effort, and this mirrors every other
        degraded-parse path in this module.
        """
        try:
            notebook = json.loads(raw_json)
        except (json.JSONDecodeError, ValueError):
            return ""

        kernel_lang = notebook.get("metadata", {}).get("kernelspec", {}).get("language", "python")
        if kernel_lang and kernel_lang.lower() not in ("python", "python3"):
            return ""

        cells = notebook.get("cells", [])
        if not isinstance(cells, list):
            return ""

        executed: list[tuple[int, str]] = []
        unexecuted: list[str] = []
        for cell in cells:
            if not isinstance(cell, dict) or cell.get("cell_type") != "code":
                continue
            source = cell.get("source", "")
            text = "".join(source) if isinstance(source, list) else str(source)
            if not text.strip():
                continue
            exec_count = cell.get("execution_count")
            if isinstance(exec_count, int):
                executed.append((exec_count, text))
            else:
                unexecuted.append(text)

        executed.sort(key=lambda pair: pair[0])
        blocks = [text for _, text in executed] + unexecuted
        return "\n\n".join(blocks)

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
            metadata: dict[str, Any] = {}

            # Python types
            if lang == RepositoryLanguage.PYTHON:
                if node_type == "class_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        name = code[name_node.start_byte : name_node.end_byte]
                        kind = RepositorySymbolKind.CLASS
                        current_class = name
                        superclasses_node = node.child_by_field_name("superclasses")
                        if superclasses_node is not None:
                            bases_text = code[
                                superclasses_node.start_byte : superclasses_node.end_byte
                            ]
                            bases = [
                                b.strip() for b in bases_text.strip("()").split(",") if b.strip()
                            ]
                            if bases:
                                metadata["bases"] = bases
                elif node_type == "function_definition":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        name = code[name_node.start_byte : name_node.end_byte]
                        kind = (
                            RepositorySymbolKind.METHOD
                            if parent_class
                            else RepositorySymbolKind.FUNCTION
                        )
                        decorators = _leading_decorator_texts(code, node.start_point[0])
                        if decorators:
                            metadata["decorators"] = decorators
                        if name == "__init__":
                            params_node = node.child_by_field_name("parameters")
                            if params_node is not None:
                                params_text = code[params_node.start_byte : params_node.end_byte]
                                param_types = re.findall(r":\s*([\w.]+)", params_text)
                                param_types = [t for t in param_types if t != "self"]
                                if param_types:
                                    metadata["param_types"] = param_types
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
                        metadata=metadata,
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
                bases = _dotted_names(node.bases)
                symbols.append(
                    RepositorySymbol(
                        name=node.name,
                        kind=RepositorySymbolKind.CLASS,
                        file_path=file_path_str,
                        line_start=getattr(node, "lineno", 1),
                        line_end=getattr(node, "end_lineno", getattr(node, "lineno", 1)),
                        docstring=doc,
                        metadata={"bases": bases} if bases else {},
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
                decorators = _dotted_names(node.decorator_list)
                metadata: dict[str, Any] = {}
                if decorators:
                    metadata["decorators"] = decorators
                if node.name == "__init__":
                    # Constructor parameter type annotations — the
                    # dependency-injection shape signal (a class whose
                    # __init__ takes typed collaborators like `SomeService`)
                    # independent of the class's own name/folder location.
                    param_types = [
                        n
                        for arg in node.args.args
                        if arg.arg != "self" and arg.annotation is not None
                        for n in _dotted_names([arg.annotation])
                    ]
                    if param_types:
                        metadata["param_types"] = param_types
                symbols.append(
                    RepositorySymbol(
                        name=node.name,
                        kind=kind,
                        file_path=file_path_str,
                        line_start=node.lineno,
                        line_end=getattr(node, "end_lineno", node.lineno),
                        docstring=doc,
                        parent=self.class_stack[-1] if self.class_stack else None,
                        metadata=metadata,
                    )
                )
                self.generic_visit(node)

        PythonVisitor().visit(tree)
        return symbols, edges

    # Languages with a dedicated, reasonably-precise pattern set below. Any
    # language tagged by EXTENSION_LANGUAGE_MAP but absent from this dict
    # falls through to _GENERIC_FALLBACK_PATTERNS instead of yielding zero
    # symbols — the "no generic path for unsupported languages" gap named in
    # the repository-intelligence baseline audit §8/Recommendation 4.
    _LANGUAGE_PATTERNS: dict[RepositoryLanguage, list[tuple[str, RepositorySymbolKind]]] = {
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
        RepositoryLanguage.JAVA: [
            (
                r"(?:public\s+|final\s+|abstract\s+)*(?:class|interface|record|enum)\s+(\w+)",
                RepositorySymbolKind.CLASS,
            ),
            (
                # method: modifiers + return type + name(  — conservative,
                # anchored on an opening brace to skip declarations.
                r"(?:public|protected|private|static)[\w\s<>\[\],]*?\s(\w+)\s*\([^;{)]*\)[\w\s,]*\{",
                RepositorySymbolKind.FUNCTION,
            ),
            (r"import\s+(?:static\s+)?([\w.]+(?:\.\*)?);", RepositorySymbolKind.IMPORT),
        ],
        RepositoryLanguage.CPP: [
            (r"(?:class|struct)\s+(\w+)\s*[:{]", RepositorySymbolKind.CLASS),
            (
                # free function / method definition: name( ... ) {
                r"[\w:<>*&~\]\[]+\s+([\w:~]+)\s*\([^;{)]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{",
                RepositorySymbolKind.FUNCTION,
            ),
            (r"#include\s+[<\"]([^>\"]+)[>\"]", RepositorySymbolKind.IMPORT),
        ],
        RepositoryLanguage.CSHARP: [
            (
                r"(?:public\s+|private\s+|internal\s+|protected\s+|static\s+|sealed\s+|abstract\s+|partial\s+)*(?:class|interface|struct|record|enum)\s+(\w+)",
                RepositorySymbolKind.CLASS,
            ),
            (
                r"(?:public|private|internal|protected|static)[\w\s<>\[\],?]*?\s(\w+)\s*\([^;{)]*\)\s*\{",
                RepositorySymbolKind.FUNCTION,
            ),
            (r"using\s+([\w.]+)\s*;", RepositorySymbolKind.IMPORT),
        ],
        RepositoryLanguage.PHP: [
            (
                r"(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+(\w+)",
                RepositorySymbolKind.CLASS,
            ),
            (
                r"(?:public\s+|private\s+|protected\s+|static\s+)*function\s+&?(\w+)\s*\(",
                RepositorySymbolKind.FUNCTION,
            ),
            (
                r"(?:require|require_once|include|include_once)\s*\(?['\"]([^'\"]+)['\"]",
                RepositorySymbolKind.IMPORT,
            ),
        ],
        RepositoryLanguage.RUBY: [
            (r"\b(?:class|module)\s+(\w+(?:::\w+)*)", RepositorySymbolKind.CLASS),
            (r"\bdef\s+(?:self\.)?(\w+[?!=]?)", RepositorySymbolKind.FUNCTION),
            (r"\brequire(?:_relative)?\s+['\"]([^'\"]+)['\"]", RepositorySymbolKind.IMPORT),
        ],
        RepositoryLanguage.SWIFT: [
            (
                r"(?:public\s+|private\s+|internal\s+|final\s+|open\s+)*(?:class|struct|protocol|enum|extension)\s+(\w+)",
                RepositorySymbolKind.CLASS,
            ),
            (
                r"(?:public\s+|private\s+|internal\s+|static\s+|override\s+)*func\s+(\w+)",
                RepositorySymbolKind.FUNCTION,
            ),
            (r"import\s+(\w+)", RepositorySymbolKind.IMPORT),
        ],
        RepositoryLanguage.KOTLIN: [
            (
                r"(?:public\s+|private\s+|internal\s+|open\s+|abstract\s+|sealed\s+|data\s+)*(?:class|interface|object|enum\s+class)\s+(\w+)",
                RepositorySymbolKind.CLASS,
            ),
            (
                r"(?:public\s+|private\s+|internal\s+|override\s+|suspend\s+)*fun\s+(?:<[^>]+>\s*)?(\w+)",
                RepositorySymbolKind.FUNCTION,
            ),
            (r"import\s+([\w.]+)", RepositorySymbolKind.IMPORT),
        ],
        RepositoryLanguage.DART: [
            (
                r"(?:abstract\s+)?(?:class|mixin|enum)\s+(\w+)",
                RepositorySymbolKind.CLASS,
            ),
            (
                r"(?:static\s+|final\s+)?[\w<>?]+\s+(\w+)\s*\([^;{)]*\)\s*(?:async\s*)?\{",
                RepositorySymbolKind.FUNCTION,
            ),
            (r"import\s+['\"]([^'\"]+)['\"]", RepositorySymbolKind.IMPORT),
        ],
        RepositoryLanguage.SCALA: [
            (
                r"(?:sealed\s+|abstract\s+|final\s+|case\s+)*(?:class|trait|object)\s+(\w+)",
                RepositorySymbolKind.CLASS,
            ),
            (r"def\s+(\w+)", RepositorySymbolKind.FUNCTION),
            (r"import\s+([\w.{}, ]+)", RepositorySymbolKind.IMPORT),
        ],
        # Schema/DDL DSLs — no functions/classes, but their top-level
        # declarations (tables, types, messages, models) are the meaningful
        # unit and are captured as CLASS-kind symbols.
        RepositoryLanguage.SQL: [
            (
                r"CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)",
                RepositorySymbolKind.CLASS,
            ),
            (r"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+[`\"\[]?(\w+)", RepositorySymbolKind.CLASS),
            (
                r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+[`\"\[]?(\w+)",
                RepositorySymbolKind.FUNCTION,
            ),
        ],
        RepositoryLanguage.GRAPHQL: [
            (r"\btype\s+(\w+)", RepositorySymbolKind.CLASS),
            (r"\binterface\s+(\w+)", RepositorySymbolKind.CLASS),
            (r"\benum\s+(\w+)", RepositorySymbolKind.CLASS),
            (r"\binput\s+(\w+)", RepositorySymbolKind.CLASS),
        ],
        RepositoryLanguage.PROTO: [
            (r"\bmessage\s+(\w+)", RepositorySymbolKind.CLASS),
            (r"\bservice\s+(\w+)", RepositorySymbolKind.CLASS),
            (r"\benum\s+(\w+)", RepositorySymbolKind.CLASS),
            (r"\brpc\s+(\w+)", RepositorySymbolKind.FUNCTION),
        ],
        RepositoryLanguage.PRISMA: [
            (r"\bmodel\s+(\w+)", RepositorySymbolKind.CLASS),
            (r"\benum\s+(\w+)", RepositorySymbolKind.CLASS),
        ],
    }

    # True universal fallback: applied to any tagged language with no entry
    # above (Objective-C, Scala's cousins, Clojure, Elixir, Erlang, Haskell,
    # OCaml, F#, Lua, R, Julia, shell, PowerShell, Vue/Svelte SFCs, ...).
    # Deliberately broad rather than per-language-precise — a low-confidence
    # signal is the explicit goal here, not a grammar. Every symbol this
    # produces is tagged metadata={"extraction": "generic_fallback"} so a
    # downstream confidence layer can discount it relative to a tree-sitter/
    # AST/dedicated-regex result.
    _GENERIC_FALLBACK_PATTERNS: list[tuple[str, RepositorySymbolKind]] = [
        (
            r"\b(?:class|struct|interface|trait|protocol|module|record|defmodule)\s+(\w+)",
            RepositorySymbolKind.CLASS,
        ),
        (
            r"\b(?:def|fn|func|function|sub|defn|defun)\s+(\w+[?!]?)",
            RepositorySymbolKind.FUNCTION,
        ),
        (
            r"^\s*(\w+)\s*(?:<-|=)\s*function\s*\(",  # R: name <- function(...)
            RepositorySymbolKind.FUNCTION,
        ),
        (
            r"\b(?:import|require|require_relative|use|include|include_once|open)\s+['\"]?([\w./:-]+)['\"]?",
            RepositorySymbolKind.IMPORT,
        ),
    ]

    # Languages with no meaningful function/class symbol concept at all —
    # markup/templates, not code in the sense the rest of this parser
    # models. Zero symbols here is a correct answer, not a gap.
    _NO_SYMBOL_LANGUAGES = frozenset({RepositoryLanguage.HTML, RepositoryLanguage.TEMPLATE})

    def _parse_regex(
        self, file_path: Path, code: str, lang: RepositoryLanguage
    ) -> tuple[list[RepositorySymbol], list[RepositoryEdge]]:
        """Regex symbol extractor: per-language patterns, or a generic fallback.

        Every ``EXTENSION_LANGUAGE_MAP``-tagged language reaches either a
        dedicated pattern set above or ``_GENERIC_FALLBACK_PATTERNS`` — the
        prior behavior (silently zero symbols for anything outside a fixed
        enumeration) is what let a legacy PHP codebase, for instance, index
        as "7 files, 0 symbols, language: unknown" with no signal that the
        tool simply didn't support it (baseline §6.2).
        """
        if lang in self._NO_SYMBOL_LANGUAGES:
            return [], []

        patterns = self._LANGUAGE_PATTERNS.get(lang)
        generic = patterns is None
        if patterns is None:
            patterns = self._GENERIC_FALLBACK_PATTERNS

        symbols: list[RepositorySymbol] = []
        edges: list[RepositoryEdge] = []
        file_path_str = str(file_path)
        seen: set[tuple[int, str, RepositorySymbolKind]] = set()

        for pattern, kind in patterns:
            for match in re.finditer(pattern, code, re.MULTILINE):
                value = match.group(1).strip()
                if not value:
                    continue
                start_char = match.start()
                line_no = code[:start_char].count("\n") + 1

                # The generic fallback's patterns can double-match the same
                # declaration (e.g. a name matching both the class and
                # function keyword lists in a language we don't actually
                # know the grammar of) — dedupe by (line, name, kind).
                dedupe_key = (line_no, value, kind)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)

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
                        metadata={"extraction": "generic_fallback"} if generic else {},
                    )
                )

        return symbols, edges
