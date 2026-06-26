"""Tree-sitter-based AST parsing for symbol extraction.

Tree-sitter ships compiled C extensions (.pyd/.so).  Importing them at module
load time forces Windows to load several DLLs synchronously — each triggering
a Defender real-time scan — adding seconds to *every* startup, even when no
parsing is requested.  We therefore defer all tree-sitter imports until the
first actual parse via ``_ensure_tree_sitter()``.

``HAS_TREE_SITTER`` starts ``None`` (unknown) and becomes ``True``/``False``
after the first lazy load attempt.  It remains importable for callers/tests
that reference it, but no import cost is paid until parsing happens.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from velune._compat import StrEnum

if TYPE_CHECKING:
    from tree_sitter import Tree

logger = logging.getLogger("velune.repository.ast_parser")

# ---------------------------------------------------------------------------
# Lazy tree-sitter loader — no DLL cost until first parse
# ---------------------------------------------------------------------------

#: ``None`` = not yet probed; ``True`` = available; ``False`` = unavailable.
HAS_TREE_SITTER: bool | None = None
_TS_LANGUAGES: dict[str, Any] = {}
# We do NOT cache a singleton Parser — each parse invocation creates a fresh
# Parser (cheap object) and sets its language, which is safe in a thread pool.
_TS_PARSER_CLS: Any = None  # tree_sitter.Parser class, set after lazy load


def _ensure_tree_sitter() -> bool:
    """Lazily import tree-sitter grammars on first use.  Returns availability."""
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
        for lang_name, factory in (
            ("python", tree_sitter_python.language),
            ("typescript", tree_sitter_typescript.language_typescript),
            ("javascript", tree_sitter_typescript.language_typescript),
            ("go", tree_sitter_go.language),
            ("rust", tree_sitter_rust.language),
        ):
            try:
                lang_obj = Language(factory())
                _TS_LANGUAGES[lang_name] = lang_obj
            except Exception as exc:
                logger.debug("Failed to load %s grammar: %s", lang_name, exc)
        HAS_TREE_SITTER = True
    except ImportError:
        HAS_TREE_SITTER = False
    return HAS_TREE_SITTER


class SymbolKind(StrEnum):
    """Type of code symbol."""

    FUNCTION = "function"
    CLASS = "class"
    METHOD = "method"
    IMPORT = "import"
    VARIABLE = "variable"
    CONSTANT = "constant"
    INTERFACE = "interface"
    ENUM = "enum"
    ASYNC_FUNCTION = "async_function"


@dataclass
class Symbol:
    """Extracted code symbol from AST."""

    id: str  # Stable UUID
    name: str
    kind: SymbolKind
    file_path: str  # Relative to workspace root
    line_start: int
    line_end: int
    docstring: str | None = None
    parameters: list[str] = field(default_factory=list)
    return_type: str | None = None
    is_exported: bool = False
    source_text: str | None = None  # For reconstruction/debugging


@dataclass
class ParsedFile:
    """Result of parsing a single file."""

    file_path: Path
    language: str
    symbols: list[Symbol]
    error: str | None = None


class ASTParser:
    """Tree-sitter AST parser for multiple languages."""

    LANGUAGE_MAP = {
        "python": "tree_sitter_python",
        "javascript": "tree_sitter_typescript",
        "typescript": "tree_sitter_typescript",
        "rust": "tree_sitter_rust",
        "go": "tree_sitter_go",
    }

    def __init__(self, languages: list[str] | None = None) -> None:
        """Initialize parser with language grammars.

        Grammars are loaded lazily on first call to :meth:`parse_file` via
        :func:`_ensure_tree_sitter`.  This avoids Windows Defender DLL scans
        at startup when no parsing will be performed.

        Args:
            languages: List of language names (python, javascript, typescript,
                       rust, go).  If None, defaults to all supported languages.
        """
        self._requested_languages: list[str] = (
            list(self.LANGUAGE_MAP.keys()) if languages is None else languages
        )
        # languages and parser are populated lazily by _ensure_loaded()
        self.languages: dict[str, Any] = {}
        self.parser: Any = None
        self._loaded: bool = False

    def _ensure_loaded(self) -> bool:
        """Populate ``self.languages`` from the lazy cache.

        Returns True if at least one grammar is available.
        """
        if self._loaded:
            return bool(self.languages)
        self._loaded = True
        if _ensure_tree_sitter():
            # Restrict to languages the caller requested
            for lang in self._requested_languages:
                lang_lower = lang.lower()
                if lang_lower in _TS_LANGUAGES:
                    self.languages[lang_lower] = _TS_LANGUAGES[lang_lower]
        return bool(self.languages)

    async def parse_file(self, path: Path) -> ParsedFile | None:
        """Parse a file asynchronously without blocking event loop.

        Grammars are loaded on first call (lazy).  Runs the actual parse in a
        thread pool to avoid blocking the event loop.
        """
        try:
            # Detect language from file extension
            language = self._detect_language(path)
            if not language:
                return None

            # Trigger lazy grammar load on first call
            if not self._ensure_loaded() or language not in self.languages:
                return None

            # Read file content
            source = path.read_text(encoding="utf-8")

            # Parse in thread to avoid blocking
            loop = asyncio.get_running_loop()
            tree = await loop.run_in_executor(
                None,
                self._parse_sync,
                source,
                language,
            )

            if not tree:
                return ParsedFile(path, language, [], error="Parse failed")

            try:
                rel_path = str(path.relative_to(Path.cwd()))
            except ValueError:
                rel_path = path.name

            # Extract symbols in thread
            symbols = await loop.run_in_executor(
                None,
                self.extract_symbols,
                tree,
                source,
                language,
                rel_path,
            )

            return ParsedFile(path, language, symbols)

        except Exception as e:
            logger.error(f"Error parsing {path}: {e}")
            return None

    def _parse_sync(self, source: str, language: str) -> Any:
        """Synchronous parse operation (run in executor).

        Creates a fresh tree_sitter.Parser per call to avoid language-switching
        races when multiple files are parsed concurrently in the thread pool.
        """
        if not _TS_PARSER_CLS or language not in self.languages:
            return None

        try:
            p = _TS_PARSER_CLS()
            p.language = self.languages[language]
            tree = p.parse(source.encode("utf-8"))
            return tree
        except Exception as e:
            logger.debug(f"Parse error: {e}")
            return None

    def extract_symbols(
        self,
        tree: Tree,
        source: str,
        language: str,
        file_path: str,
    ) -> list[Symbol]:
        """Extract symbols from AST tree."""
        symbols: list[Symbol] = []

        if language == "python":
            symbols.extend(self._extract_python_symbols(tree, source, file_path))
        elif language in ("javascript", "typescript"):
            symbols.extend(self._extract_js_symbols(tree, source, file_path, language))
        elif language == "rust":
            symbols.extend(self._extract_rust_symbols(tree, source, file_path))
        elif language == "go":
            symbols.extend(self._extract_go_symbols(tree, source, file_path))

        return symbols

    def _extract_python_symbols(self, tree: Tree, source: str, file_path: str) -> list[Symbol]:
        """Extract symbols from Python AST."""
        symbols: list[Symbol] = []
        source_lines = source.split("\n")

        def visit(node: Any) -> None:
            if node.type == "function_definition":
                sym = self._parse_python_function(node, source_lines, file_path)
                if sym:
                    symbols.append(sym)
            elif node.type == "class_definition":
                sym = self._parse_python_class(node, source_lines, file_path)
                if sym:
                    symbols.append(sym)
                # Also extract methods from class
                for child in node.children:
                    if child.type == "block":
                        for subchild in child.children:
                            if subchild.type == "function_definition":
                                method = self._parse_python_function(
                                    subchild, source_lines, file_path, is_method=True
                                )
                                if method:
                                    symbols.append(method)
            elif node.type in ("import_statement", "import_from_statement"):
                sym = self._parse_python_import(node, source_lines, file_path)
                if sym:
                    symbols.append(sym)

            for child in node.children:
                visit(child)

        visit(tree.root_node)
        return symbols

    def _parse_python_function(
        self,
        node: Any,
        source_lines: list[str],
        file_path: str,
        is_method: bool = False,
    ) -> Symbol | None:
        """Parse a Python function node."""
        try:
            name_node = None
            params_node = None
            is_async = False

            # Check if async
            for child in node.children:
                if child.type == "async":
                    is_async = True

            # Find name and parameters
            for child in node.children:
                if child.type == "identifier":
                    name_node = child
                elif child.type == "parameters":
                    params_node = child

            if not name_node:
                return None

            name = self._get_node_text(name_node, source_lines)
            params = self._extract_parameters(params_node, source_lines) if params_node else []
            docstring = self._extract_python_docstring(node, source_lines)

            kind = (
                SymbolKind.ASYNC_FUNCTION
                if is_async
                else SymbolKind.METHOD
                if is_method
                else SymbolKind.FUNCTION
            )

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=kind,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                docstring=docstring,
                parameters=params,
            )
        except Exception as e:
            logger.debug(f"Error parsing Python function: {e}")
            return None

    def _parse_python_class(
        self, node: Any, source_lines: list[str], file_path: str
    ) -> Symbol | None:
        """Parse a Python class node."""
        try:
            name_node = None
            for child in node.children:
                if child.type == "identifier":
                    name_node = child
                    break

            if not name_node:
                return None

            name = self._get_node_text(name_node, source_lines)
            docstring = self._extract_python_docstring(node, source_lines)

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=SymbolKind.CLASS,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                docstring=docstring,
            )
        except Exception as e:
            logger.debug(f"Error parsing Python class: {e}")
            return None

    def _parse_python_import(
        self, node: Any, source_lines: list[str], file_path: str
    ) -> Symbol | None:
        """Parse a Python import statement."""
        try:
            text = self._get_node_text(node, source_lines).strip()
            # Extract imported name
            if "from" in text:
                parts = text.split("import")
                if len(parts) > 1:
                    name = parts[-1].strip().split()[0]
                else:
                    return None
            else:
                name = text.replace("import", "").strip().split()[0]

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=SymbolKind.IMPORT,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                source_text=text,
            )
        except Exception as e:
            logger.debug(f"Error parsing Python import: {e}")
            return None

    def _extract_js_symbols(
        self,
        tree: Tree,
        source: str,
        file_path: str,
        language: str,
    ) -> list[Symbol]:
        """Extract symbols from JavaScript/TypeScript AST."""
        symbols: list[Symbol] = []
        source_lines = source.split("\n")

        def visit(node: Any) -> None:
            if node.type in ("function_declaration", "generator_function_declaration"):
                sym = self._parse_js_function(node, source_lines, file_path, is_async=False)
                if sym:
                    symbols.append(sym)
            elif node.type == "class_declaration":
                sym = self._parse_js_class(node, source_lines, file_path)
                if sym:
                    symbols.append(sym)
                # Extract methods
                for child in node.children:
                    if child.type == "class_body":
                        for method_node in child.children:
                            if method_node.type in ("method_definition", "function_definition"):
                                method = self._parse_js_function(
                                    method_node, source_lines, file_path, is_method=True
                                )
                                if method:
                                    symbols.append(method)
            elif node.type in ("import_statement", "import_specifier"):
                sym = self._parse_js_import(node, source_lines, file_path)
                if sym:
                    symbols.append(sym)
            elif node.type == "export_statement":
                # Extract what's being exported
                for child in node.children:
                    if child.type in ("function_declaration", "class_declaration"):
                        visit(child)

            for child in node.children:
                visit(child)

        visit(tree.root_node)
        return symbols

    def _parse_js_function(
        self,
        node: Any,
        source_lines: list[str],
        file_path: str,
        is_async: bool = False,
        is_method: bool = False,
    ) -> Symbol | None:
        """Parse a JS/TS function node."""
        try:
            name_node = None
            params_node = None

            for child in node.children:
                if child.type == "identifier":
                    name_node = child
                elif child.type == "formal_parameters":
                    params_node = child

            if not name_node:
                return None

            name = self._get_node_text(name_node, source_lines)
            params = self._extract_parameters(params_node, source_lines) if params_node else []

            kind = (
                SymbolKind.ASYNC_FUNCTION
                if is_async
                else SymbolKind.METHOD
                if is_method
                else SymbolKind.FUNCTION
            )

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=kind,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                parameters=params,
            )
        except Exception as e:
            logger.debug(f"Error parsing JS function: {e}")
            return None

    def _parse_js_class(self, node: Any, source_lines: list[str], file_path: str) -> Symbol | None:
        """Parse a JS/TS class node."""
        try:
            name_node = None
            for child in node.children:
                if child.type == "identifier":
                    name_node = child
                    break

            if not name_node:
                return None

            name = self._get_node_text(name_node, source_lines)

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=SymbolKind.CLASS,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )
        except Exception as e:
            logger.debug(f"Error parsing JS class: {e}")
            return None

    def _parse_js_import(self, node: Any, source_lines: list[str], file_path: str) -> Symbol | None:
        """Parse a JS/TS import statement."""
        try:
            # Find imported identifiers
            text = self._get_node_text(node, source_lines).strip()
            parts = text.split("from")[0].replace("import", "").strip()
            name = parts.split(",")[0].strip().replace("{", "").replace("}", "").strip()

            if not name:
                return None

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=SymbolKind.IMPORT,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                source_text=text,
            )
        except Exception as e:
            logger.debug(f"Error parsing JS import: {e}")
            return None

    def _extract_rust_symbols(self, tree: Tree, source: str, file_path: str) -> list[Symbol]:
        """Extract symbols from Rust AST."""
        symbols: list[Symbol] = []
        source_lines = source.split("\n")

        def visit(node: Any) -> None:
            if node.type == "function_item":
                sym = self._parse_rust_function(node, source_lines, file_path)
                if sym:
                    symbols.append(sym)
            elif node.type == "struct_item":
                sym = self._parse_rust_struct(node, source_lines, file_path)
                if sym:
                    symbols.append(sym)
            elif node.type == "enum_item":
                sym = self._parse_rust_enum(node, source_lines, file_path)
                if sym:
                    symbols.append(sym)
            elif node.type == "impl_item":
                # Extract methods from impl block
                for child in node.children:
                    if child.type == "function_item":
                        method = self._parse_rust_function(
                            child, source_lines, file_path, is_method=True
                        )
                        if method:
                            symbols.append(method)

            for child in node.children:
                visit(child)

        visit(tree.root_node)
        return symbols

    def _parse_rust_function(
        self,
        node: Any,
        source_lines: list[str],
        file_path: str,
        is_method: bool = False,
    ) -> Symbol | None:
        """Parse a Rust function node."""
        try:
            name_node = None
            for child in node.children:
                if child.type == "identifier":
                    name_node = child
                    break

            if not name_node:
                return None

            name = self._get_node_text(name_node, source_lines)

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=SymbolKind.METHOD if is_method else SymbolKind.FUNCTION,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )
        except Exception as e:
            logger.debug(f"Error parsing Rust function: {e}")
            return None

    def _parse_rust_struct(
        self, node: Any, source_lines: list[str], file_path: str
    ) -> Symbol | None:
        """Parse a Rust struct node."""
        try:
            name_node = None
            for child in node.children:
                if child.type == "type_identifier":
                    name_node = child
                    break

            if not name_node:
                return None

            name = self._get_node_text(name_node, source_lines)

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=SymbolKind.CLASS,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )
        except Exception as e:
            logger.debug(f"Error parsing Rust struct: {e}")
            return None

    def _parse_rust_enum(self, node: Any, source_lines: list[str], file_path: str) -> Symbol | None:
        """Parse a Rust enum node."""
        try:
            name_node = None
            for child in node.children:
                if child.type == "type_identifier":
                    name_node = child
                    break

            if not name_node:
                return None

            name = self._get_node_text(name_node, source_lines)

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=SymbolKind.ENUM,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )
        except Exception as e:
            logger.debug(f"Error parsing Rust enum: {e}")
            return None

    def _extract_go_symbols(self, tree: Tree, source: str, file_path: str) -> list[Symbol]:
        """Extract symbols from Go AST."""
        symbols: list[Symbol] = []
        source_lines = source.split("\n")

        def visit(node: Any) -> None:
            if node.type == "function_declaration":
                sym = self._parse_go_function(node, source_lines, file_path)
                if sym:
                    symbols.append(sym)
            elif node.type == "method_declaration":
                sym = self._parse_go_function(node, source_lines, file_path, is_method=True)
                if sym:
                    symbols.append(sym)
            elif node.type == "type_declaration":
                # Handle struct/interface definitions
                for child in node.children:
                    if child.type in ("struct_type", "interface_type"):
                        kind = (
                            SymbolKind.INTERFACE
                            if child.type == "interface_type"
                            else SymbolKind.CLASS
                        )
                        sym = self._parse_go_type(child, source_lines, file_path, kind)
                        if sym:
                            symbols.append(sym)

            for child in node.children:
                visit(child)

        visit(tree.root_node)
        return symbols

    def _parse_go_function(
        self,
        node: Any,
        source_lines: list[str],
        file_path: str,
        is_method: bool = False,
    ) -> Symbol | None:
        """Parse a Go function node."""
        try:
            name_node = None
            for child in node.children:
                if child.type == "identifier":
                    name_node = child
                    break

            if not name_node:
                return None

            name = self._get_node_text(name_node, source_lines)

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=SymbolKind.METHOD if is_method else SymbolKind.FUNCTION,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )
        except Exception as e:
            logger.debug(f"Error parsing Go function: {e}")
            return None

    def _parse_go_type(
        self,
        node: Any,
        source_lines: list[str],
        file_path: str,
        kind: SymbolKind,
    ) -> Symbol | None:
        """Parse a Go struct/interface type."""
        try:
            # Go types are usually preceded by their name in a type_declaration
            # This is a simplification
            text = self._get_node_text(node, source_lines).strip()
            # Extract name from type definition
            name = text.split("{")[0].split("(")[0].strip()

            if not name:
                return None

            return Symbol(
                id=str(uuid.uuid4()),
                name=name,
                kind=kind,
                file_path=file_path,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
            )
        except Exception as e:
            logger.debug(f"Error parsing Go type: {e}")
            return None

    def _get_node_text(self, node: Any, source_lines: list[str]) -> str:
        """Extract text of a node from source."""
        try:
            start_row = node.start_point[0]
            start_col = node.start_point[1]
            end_row = node.end_point[0]
            end_col = node.end_point[1]

            if start_row == end_row:
                return source_lines[start_row][start_col:end_col]

            lines = [source_lines[start_row][start_col:]]
            for row in range(start_row + 1, end_row):
                lines.append(source_lines[row])
            lines.append(source_lines[end_row][:end_col])

            return "\n".join(lines)
        except Exception:
            return ""

    def _extract_parameters(self, params_node: Any, source_lines: list[str]) -> list[str]:
        """Extract parameter names from function parameters node."""
        try:
            text = self._get_node_text(params_node, source_lines)
            # Simple extraction: split by comma, extract identifiers
            params = []
            for part in text.replace("(", "").replace(")", "").split(","):
                part = part.strip()
                if part:
                    # Extract identifier (first word before : or =)
                    name = part.split(":")[0].split("=")[0].strip()
                    if name:
                        params.append(name)
            return params
        except Exception:
            return []

    def _extract_python_docstring(self, node: Any, source_lines: list[str]) -> str | None:
        """Extract docstring from Python function or class."""
        try:
            # Look for string node after function/class definition
            for child in node.children:
                if child.type == "block":
                    for subchild in child.children:
                        if subchild.type == "expression_statement":
                            for subsubchild in subchild.children:
                                if subsubchild.type == "string":
                                    text = self._get_node_text(subsubchild, source_lines)
                                    # Remove quotes
                                    if text.startswith('"""') or text.startswith("'''"):
                                        return text[3:-3].strip()
                                    return text.strip("\"'")
            return None
        except Exception:
            return None

    def _detect_language(self, path: Path) -> str | None:
        """Detect language from file extension."""
        ext_map = {
            ".py": "python",
            ".js": "javascript",
            ".jsx": "javascript",
            ".ts": "typescript",
            ".tsx": "typescript",
            ".rs": "rust",
            ".go": "go",
        }
        return ext_map.get(path.suffix.lower())
