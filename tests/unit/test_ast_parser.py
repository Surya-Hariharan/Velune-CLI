"""Unit tests for ASTParser (Batch 13)."""

from pathlib import Path
import pytest

from velune.repository.parser import ASTParser
from velune.repository.schemas import RepositorySymbolKind, RepositoryLanguage


def test_parse_python_class_extracts_class_symbol() -> None:
    """Verify that parsing a Python class extracts a class symbol with correct properties."""
    parser = ASTParser()
    code = "class TestClass:\n    \"\"\"Docstring for TestClass\"\"\"\n    pass\n"
    symbols, edges = parser.parse(Path("test.py"), code)
    assert len(symbols) >= 1
    cls_syms = [s for s in symbols if s.kind == RepositorySymbolKind.CLASS]
    assert len(cls_syms) == 1
    assert cls_syms[0].name == "TestClass"


def test_parse_python_function_extracts_function_symbol() -> None:
    """Verify that parsing a Python function extracts a function symbol."""
    parser = ASTParser()
    code = "def test_func(x):\n    \"\"\"Docstring for test_func\"\"\"\n    return x + 1\n"
    symbols, edges = parser.parse(Path("test.py"), code)
    assert len(symbols) >= 1
    func_syms = [s for s in symbols if s.kind == RepositorySymbolKind.FUNCTION]
    assert len(func_syms) == 1
    assert func_syms[0].name == "test_func"


def test_parse_python_import_extracts_import_and_edge() -> None:
    """Verify that parsing imports in Python extracts import symbols and edges."""
    parser = ASTParser()
    code = "import os\nfrom math import sin\n"
    symbols, edges = parser.parse(Path("test.py"), code)
    
    import_syms = [s for s in symbols if s.kind == RepositorySymbolKind.IMPORT]
    assert len(import_syms) >= 2
    assert any(s.name == "os" for s in import_syms)
    assert any(s.name in ("math", "sin") for s in import_syms)
    
    assert len(edges) >= 2
    assert any(e.target == "os" for e in edges)
    assert any(e.target in ("math", "math.sin") for e in edges)


def test_parse_python_method_has_parent_set() -> None:
    """Verify that methods inside a Python class have their parent field set to the class name."""
    parser = ASTParser()
    code = "class OuterClass:\n    def inner_method(self):\n        pass\n"
    symbols, edges = parser.parse(Path("test.py"), code)
    
    method_syms = [s for s in symbols if s.kind == RepositorySymbolKind.METHOD]
    assert len(method_syms) == 1
    assert method_syms[0].name == "inner_method"
    assert method_syms[0].parent == "OuterClass"


def test_parse_invalid_syntax_returns_empty_not_raises() -> None:
    """Verify that parsing invalid syntax does not raise exceptions and returns empty results."""
    parser = ASTParser()
    code = "!!!@@@###$$$%%%"
    symbols, edges = parser.parse(Path("test.py"), code)
    assert isinstance(symbols, list)
    assert isinstance(edges, list)
    assert len(symbols) == 0
    assert len(edges) == 0


def test_detect_language_by_extension() -> None:
    """Verify extension-based language detection works for py, ts, go, rs."""
    parser = ASTParser()
    assert parser._detect_language(Path("file.py")) == RepositoryLanguage.PYTHON
    assert parser._detect_language(Path("file.ts")) == RepositoryLanguage.TYPESCRIPT
    assert parser._detect_language(Path("file.go")) == RepositoryLanguage.GO
    assert parser._detect_language(Path("file.rs")) == RepositoryLanguage.RUST
    assert parser._detect_language(Path("file.unknown")) == RepositoryLanguage.UNKNOWN


def test_ast_parser_python_fallback() -> None:
    """Verify that Python AST fallback parsing functions correctly when tree-sitter is disabled."""
    parser = ASTParser()
    # Disable tree-sitter path. Languages now load lazily on first parse, so we
    # mark the parser as already-loaded with an empty language map to force the
    # AST/regex fallbacks.
    parser._loaded = True
    parser.languages = {}
    
    code = (
        "import os\n"
        "from math import sin\n"
        "class MyClass:\n"
        "    def method(self):\n"
        "        pass\n"
    )
    symbols, edges = parser.parse(Path("test.py"), code)
    
    assert len(symbols) >= 3
    assert any(s.name == "MyClass" and s.kind == RepositorySymbolKind.CLASS for s in symbols)
    assert any(s.name == "method" and s.kind == RepositorySymbolKind.METHOD and s.parent == "MyClass" for s in symbols)
    assert any(s.name == "sin" and s.kind == RepositorySymbolKind.IMPORT for s in symbols)
    assert any(e.target == "math.sin" for e in edges)


def test_ast_parser_regex_fallbacks() -> None:
    """Verify that regex fallback parsing works for TypeScript, Go, and Rust when tree-sitter is disabled."""
    parser = ASTParser()
    # Disable tree-sitter path (lazy-load aware — see test_ast_parser_python_fallback).
    parser._loaded = True
    parser.languages = {}
    
    # 1. TypeScript
    ts_code = "export class Controller {}\nexport async function handler() {}\nimport { log } from 'logger';\n"
    symbols, edges = parser.parse(Path("test.ts"), ts_code)
    assert any(s.name == "Controller" and s.kind == RepositorySymbolKind.CLASS for s in symbols)
    assert any(s.name == "handler" and s.kind == RepositorySymbolKind.FUNCTION for s in symbols)
    assert any(e.target == "logger" for e in edges)
    
    # 2. Go
    go_code = "type Service struct {}\nfunc DoWork() {}\nimport \"fmt\"\n"
    symbols, edges = parser.parse(Path("test.go"), go_code)
    assert any(s.name == "Service" and s.kind == RepositorySymbolKind.CLASS for s in symbols)
    assert any(s.name == "DoWork" and s.kind == RepositorySymbolKind.FUNCTION for s in symbols)
    assert any(e.target == "fmt" for e in edges)
    
    # 3. Rust
    rs_code = "pub struct Config {}\npub fn build() {}\nuse std::io;\n"
    symbols, edges = parser.parse(Path("test.rs"), rs_code)
    assert any(s.name == "Config" and s.kind == RepositorySymbolKind.CLASS for s in symbols)
    assert any(s.name == "build" and s.kind == RepositorySymbolKind.FUNCTION for s in symbols)
    assert any(e.target == "std::io" for e in edges)


def test_ast_parser_tree_sitter_other_languages() -> None:
    """Verify that tree-sitter parses TypeScript, Go, and Rust successfully when enabled."""
    parser = ASTParser()
    if not parser.languages:
        pytest.skip("Tree-sitter languages not loaded")
        
    # 1. TypeScript
    ts_code = "export class Controller {}\nexport async function handler() {}\nimport { log } from 'logger';\n"
    symbols, edges = parser.parse(Path("test.ts"), ts_code)
    assert len(symbols) >= 1
    
    # 2. Go
    go_code = "type Service struct {}\nfunc DoWork() {}\nimport \"fmt\"\n"
    symbols, edges = parser.parse(Path("test.go"), go_code)
    assert len(symbols) >= 1
    
    # 3. Rust
    rs_code = "pub struct Config {}\npub fn build() {}\nuse std::io;\n"
    symbols, edges = parser.parse(Path("test.rs"), rs_code)
    assert len(symbols) >= 1
