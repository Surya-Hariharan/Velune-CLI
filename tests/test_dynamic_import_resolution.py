"""Unit tests for dynamic-import detection and resolution.

``docs/repository-intelligence-baseline.md`` §6.7/§8: "Static-only import
resolution with no dynamic-import awareness at all... Plugin systems,
dependency-injection containers, and lazy-loaded route registries are
common precisely in larger, more mature codebases — and precisely those
codebases will have the emptiest, least-useful dependency graphs from this
pipeline." Every AST/tree-sitter/regex backend in parser.py only ever
recognized static import *statements*, never dynamic-loading call
*expressions* like ``importlib.import_module(...)`` or ``require(var)``.

Two pieces close this gap:
- ``RepositorySnapshotParser._extract_dynamic_imports`` (parser.py) detects
  the call sites and classifies the argument by how much literal
  information it carries.
- ``RepositoryGrapher._add_dynamic_prefix_edges`` (grapher.py) expands a
  partial literal prefix to every real file under it, at a lower
  confidence weight than a fully static import.
"""

from __future__ import annotations

from pathlib import Path

from velune.repository.grapher import RepositoryGrapher
from velune.repository.parser import RepositorySnapshotParser
from velune.repository.schemas import RepositorySymbolKind

parser = RepositorySnapshotParser()


class TestClassifyDynamicImportArg:
    def test_plain_string_literal_is_fully_confident_and_not_dynamic(self):
        target, confidence, is_dynamic = parser._classify_dynamic_import_arg('"plugins.foo"')
        assert target == "plugins.foo"
        assert confidence == 1.0
        assert is_dynamic is False

    def test_fstring_with_literal_prefix_is_partial_confidence(self):
        target, confidence, is_dynamic = parser._classify_dynamic_import_arg('f"plugins.{name}"')
        assert target == "plugins"
        assert 0 < confidence < 1.0
        assert is_dynamic is True

    def test_template_literal_with_literal_prefix(self):
        target, confidence, is_dynamic = parser._classify_dynamic_import_arg("`plugins/${name}`")
        assert target == "plugins"
        assert is_dynamic is True

    def test_bare_identifier_has_no_target_at_all(self):
        target, confidence, is_dynamic = parser._classify_dynamic_import_arg("modPath")
        assert target is None
        assert is_dynamic is True
        assert confidence < 0.5


class TestExtractDynamicImports:
    def test_python_import_module_literal_treated_as_static(self):
        code = 'import importlib\nimportlib.import_module("plugins.plugin_a")\n'
        symbols, edges = parser.parse(Path("loader.py"), code)
        dynamic_syms = [s for s in symbols if s.kind == RepositorySymbolKind.IMPORT]
        assert any(s.name == "plugins.plugin_a" for s in dynamic_syms)
        assert any(e.target == "plugins.plugin_a" and e.edge_type == "imports" for e in edges)

    def test_python_import_module_fstring_is_dynamic_with_prefix(self):
        code = 'import importlib\n\n\ndef load(name):\n    importlib.import_module(f"plugins.{name}")\n'
        symbols, edges = parser.parse(Path("loader.py"), code)
        dyn_edges = [e for e in edges if e.edge_type == "imports_dynamic"]
        assert any(e.target == "plugins" for e in dyn_edges)
        assert all(e.weight < 1.0 for e in dyn_edges)

    def test_python_dunder_import_bare_variable_records_symbol_no_edge(self):
        code = "def load(mod_name):\n    __import__(mod_name)\n"
        symbols, edges = parser.parse(Path("loader.py"), code)
        assert any(
            s.kind == RepositorySymbolKind.IMPORT and s.metadata.get("dynamic") for s in symbols
        )
        assert not any(e.edge_type == "imports_dynamic" for e in edges)

    def test_js_require_literal_produces_normal_static_edge(self):
        code = "const foo = require('./foo');\n"
        symbols, edges = parser.parse(Path("index.js"), code)
        assert any(e.target == "./foo" and e.edge_type == "imports" for e in edges)

    def test_js_require_bare_variable_is_dynamic_unresolved(self):
        code = "function load(modPath) {\n  const mod = require(modPath);\n  return mod;\n}\n"
        symbols, edges = parser.parse(Path("loader.js"), code)
        assert any(
            s.kind == RepositorySymbolKind.IMPORT and s.name == "<dynamic import>"
            for s in symbols
        )
        assert not any(e.edge_type == "imports_dynamic" for e in edges)

    def test_ruby_has_no_dynamic_import_pattern_registered(self):
        """Only Python/JS/TS have a dynamic-import call shape registered —
        Ruby's require(var) idiom isn't covered by this pass, and that
        should mean zero *extra* symbols, not a crash."""
        code = "def load(name)\n  require name\nend\n"
        symbols, edges = parser.parse(Path("loader.rb"), code)
        assert not any(s.metadata.get("dynamic") for s in symbols)


class TestGrapherDynamicPrefixExpansion:
    def test_prefix_expands_to_all_matching_files_via_dotted_module(self):
        grapher = RepositoryGrapher(Path("."))
        file_by_mod = {
            "plugins.plugin_a": "plugins/plugin_a.py",
            "plugins.plugin_b": "plugins/plugin_b.py",
            "plugins": "plugins/__init__.py",
            "other.module": "other/module.py",
        }
        grapher._add_dynamic_prefix_edges("loader.py", "plugins", 0.5, file_by_mod, {})
        targets = {
            data["edge_type"]: None
            for _, _, data in grapher.graph.out_edges("loader.py", data=True)
        }
        assert "imports_dynamic" in targets

        edge_targets = {tgt for _, tgt in grapher.graph.out_edges("loader.py")}
        assert "plugins/plugin_a.py" in edge_targets
        assert "plugins/plugin_b.py" in edge_targets
        assert "plugins/__init__.py" in edge_targets
        assert "other/module.py" not in edge_targets

    def test_no_duplicate_edges_when_multiple_mod_keys_share_a_target(self):
        """plugins and plugins.__init__ both legitimately point at
        plugins/__init__.py — only one edge should be added."""
        grapher = RepositoryGrapher(Path("."))
        file_by_mod = {
            "plugins": "plugins/__init__.py",
            "plugins.__init__": "plugins/__init__.py",
        }
        grapher._add_dynamic_prefix_edges("loader.py", "plugins", 0.5, file_by_mod, {})
        edges = list(grapher.graph.out_edges("loader.py"))
        assert len(edges) == 1

    def test_falls_back_to_path_stem_matching_when_no_mod_match(self):
        grapher = RepositoryGrapher(Path("."))
        file_by_stem = {"plugins/plugin_a": "plugins/plugin_a.js"}
        grapher._add_dynamic_prefix_edges("loader.js", "plugins", 0.5, {}, file_by_stem)
        edge_targets = {tgt for _, tgt in grapher.graph.out_edges("loader.js")}
        assert "plugins/plugin_a.js" in edge_targets
