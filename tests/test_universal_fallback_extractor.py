"""Unit tests for the universal low-confidence fallback symbol extractor.

``docs/repository-intelligence-baseline.md`` §8 (Weakness 2): "No generic/
fallback path for unsupported languages or frameworks. Every extraction
step ... is a closed enumeration; anything outside it contributes zero
information." Confirmed live in §6.2 (PHP: "All 3 PHP files get 0 symbols").

``RepositorySnapshotParser._parse_regex`` now has three tiers:
1. A dedicated, reasonably-precise pattern set for languages close enough
   to the C-family class/function idiom (PHP, Ruby, Swift, Kotlin, C#,
   Dart, Scala) or with a meaningful schema-DSL declaration shape (SQL,
   GraphQL, Protobuf, Prisma).
2. A true generic fallback (``_GENERIC_FALLBACK_PATTERNS``) for anything
   else tagged by ``EXTENSION_LANGUAGE_MAP`` with no dedicated set —
   Objective-C, Scala's functional cousins, Lua, R, shell, PowerShell, etc.
   Every symbol from this tier carries
   ``metadata={"extraction": "generic_fallback"}`` so a confidence layer
   can discount it later.
3. Markup/template languages (HTML, Jinja) with no function/class concept
   at all correctly yield zero symbols — that's a right answer, not a gap.
"""

from __future__ import annotations

from pathlib import Path

from velune.repository.parser import RepositorySnapshotParser
from velune.repository.schemas import RepositorySymbolKind

parser = RepositorySnapshotParser()


def _names(code: str, path: str, kind: RepositorySymbolKind | None = None) -> list[str]:
    symbols, _ = parser.parse(Path(path), code)
    if kind is not None:
        symbols = [s for s in symbols if s.kind == kind]
    return [s.name for s in symbols]


def test_php_class_function_and_import():
    code = (
        "<?php\nclass UserRepo {\n    public function find($id) {\n"
        "        return null;\n    }\n}\nrequire_once 'db.php';\n"
    )
    symbols, edges = parser.parse(Path("repo.php"), code)
    assert "UserRepo" in [s.name for s in symbols if s.kind == RepositorySymbolKind.CLASS]
    assert "find" in [s.name for s in symbols if s.kind == RepositorySymbolKind.FUNCTION]
    assert any(e.target == "db.php" for e in edges)


def test_ruby_module_class_def():
    code = "module Shop\n  class Cart\n    def total\n      0\n    end\n  end\nend\n"
    assert "Cart" in _names(code, "cart.rb", RepositorySymbolKind.CLASS)
    assert "total" in _names(code, "cart.rb", RepositorySymbolKind.FUNCTION)


def test_swift_class_and_func():
    code = "import Foundation\nclass Greeter {\n    func hello() -> String { return \"hi\" }\n}\n"
    assert "Greeter" in _names(code, "g.swift", RepositorySymbolKind.CLASS)
    assert "hello" in _names(code, "g.swift", RepositorySymbolKind.FUNCTION)


def test_kotlin_class_and_fun():
    code = "class Greeter {\n    fun hello(): String { return \"hi\" }\n}\n"
    assert "Greeter" in _names(code, "g.kt", RepositorySymbolKind.CLASS)
    assert "hello" in _names(code, "g.kt", RepositorySymbolKind.FUNCTION)


def test_csharp_class_and_method():
    code = "public class Greeter {\n    public string Hello() {\n        return \"hi\";\n    }\n}\n"
    assert "Greeter" in _names(code, "g.cs", RepositorySymbolKind.CLASS)
    assert "Hello" in _names(code, "g.cs", RepositorySymbolKind.FUNCTION)


def test_dart_class_and_function():
    code = "class Greeter {\n  String hello() {\n    return 'hi';\n  }\n}\n"
    assert "Greeter" in _names(code, "g.dart", RepositorySymbolKind.CLASS)


def test_sql_table_view_function():
    code = (
        "CREATE TABLE users (id INT);\n"
        "CREATE VIEW active_users AS SELECT * FROM users;\n"
        "CREATE FUNCTION get_user() RETURNS INT AS $$ SELECT 1 $$;\n"
    )
    classes = _names(code, "schema.sql", RepositorySymbolKind.CLASS)
    funcs = _names(code, "schema.sql", RepositorySymbolKind.FUNCTION)
    assert "users" in classes
    assert "active_users" in classes
    assert "get_user" in funcs


def test_graphql_type_and_enum():
    code = "type User {\n  id: ID!\n}\nenum Role {\n  ADMIN\n  USER\n}\n"
    classes = _names(code, "schema.graphql", RepositorySymbolKind.CLASS)
    assert "User" in classes
    assert "Role" in classes


def test_protobuf_message_and_service():
    code = "message User {\n  string id = 1;\n}\nservice UserService {\n  rpc Get(User) returns (User);\n}\n"
    classes = _names(code, "user.proto", RepositorySymbolKind.CLASS)
    funcs = _names(code, "user.proto", RepositorySymbolKind.FUNCTION)
    assert "User" in classes
    assert "UserService" in classes
    assert "Get" in funcs


def test_prisma_model():
    code = "model User {\n  id Int @id\n}\n"
    assert "User" in _names(code, "schema.prisma", RepositorySymbolKind.CLASS)


def test_generic_fallback_covers_languages_with_no_dedicated_pattern():
    """Lua, R, and Elixir have no dedicated pattern set — they must still
    produce symbols via the generic fallback, tagged as such."""
    lua_code = "function greet()\n  return 1\nend\n"
    symbols, _ = parser.parse(Path("g.lua"), lua_code)
    assert [s.name for s in symbols] == ["greet"]
    assert symbols[0].metadata.get("extraction") == "generic_fallback"

    elixir_code = "defmodule Greeter do\n  def hello do\n    1\n  end\nend\n"
    symbols, _ = parser.parse(Path("g.ex"), elixir_code)
    names = [s.name for s in symbols]
    assert "Greeter" in names
    assert "hello" in names
    assert all(s.metadata.get("extraction") == "generic_fallback" for s in symbols)


def test_r_assignment_style_function_via_generic_fallback():
    code = "greet <- function(name) {\n  return(name)\n}\n"
    symbols, _ = parser.parse(Path("g.r"), code)
    assert "greet" in [s.name for s in symbols]


def test_html_and_template_have_no_symbol_concept_and_that_is_correct():
    """Markup/template languages genuinely have no function/class symbols —
    zero here is a right answer, not a silent gap."""
    html = "<html><body><h1>Hi</h1></body></html>"
    symbols, edges = parser.parse(Path("index.html"), html)
    assert symbols == []
    assert edges == []

    jinja = "{% for x in items %}{{ x }}{% endfor %}"
    symbols, edges = parser.parse(Path("page.jinja2"), jinja)
    assert symbols == []
    assert edges == []


def test_dedicated_patterns_are_not_tagged_generic_fallback():
    """A language with its own pattern set (PHP here) must not carry the
    generic_fallback marker — that's reserved for the true catch-all."""
    code = "<?php\nclass Foo {}\n"
    symbols, _ = parser.parse(Path("f.php"), code)
    assert symbols and all("extraction" not in s.metadata for s in symbols)
