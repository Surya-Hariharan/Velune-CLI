"""Regression coverage for the six previously-untested classification modules.

``docs/REPOSITORY_INTELLIGENCE_BASELINE.md`` §8 flagged api_mapper.py,
architecture_detector.py, technology_detector.py, project_type.py,
config_intelligence.py, and analyzer.py's classification logic as having
*no dedicated test files*, despite being the modules responsible for nearly
every finding in the baseline's 8-repo benchmark (§6). This backfills that
gap using the same repo-style corpus (``tests/fixtures/repo_styles.py``).

Some assertions here are deliberately *characterization tests*: they pin a
known, still-open gap (dynamic imports, oversized vendored files, Jupyter
notebooks, single-valued tech stack on polyglot repos) rather than ideal
behavior, so a future fix changes a visible assertion instead of silently
drifting. They are marked with a comment naming the gap.
"""

from __future__ import annotations

from pathlib import Path

from tests.fixtures.repo_styles import materialize

from velune.repository.analyzer import CodebaseAnalyzer
from velune.repository.api_mapper import APIMapper
from velune.repository.architecture_detector import ArchitectureDetector
from velune.repository.cognition import RepositoryCognitionService
from velune.repository.config_intelligence import ConfigIntelligenceExtractor
from velune.repository.project_type import ProjectType, ProjectTypeDetector
from velune.repository.scanner import FilesystemScanner
from velune.repository.schemas import RepositoryLanguage
from velune.repository.technology_detector import TechnologyDetector

# ── well_structured: the pipeline's home turf — everything should work ─────


def test_well_structured_end_to_end(tmp_path):
    root = materialize("well_structured", tmp_path)
    svc = RepositoryCognitionService(root)
    snapshot = svc.index(force=True)
    arch = snapshot.summary["architecture"]

    assert arch["tech_stack"]["language"] == "Python"
    assert arch["tech_stack"]["framework"] == "FastAPI"
    assert arch["arch_report"]["pattern"] == "FastAPI MVC"
    assert arch["api_map"] if "api_map" in arch else True  # arch_report path only
    assert snapshot.api_map is not None and len(snapshot.api_map.routes) >= 1

    # Secret file must never be indexed.
    indexed_paths = {f.path.replace("\\", "/") for f in snapshot.files}
    assert ".env" not in indexed_paths


def test_well_structured_config_intelligence(tmp_path):
    root = materialize("well_structured", tmp_path)
    intel = ConfigIntelligenceExtractor().extract(root, ["pyproject.toml"])
    assert intel.test_cmd == "pytest -q"  # addopts from [tool.pytest.ini_options]
    assert intel.lint_cmd == "ruff check ."
    assert "fastapi" in [d.lower() for d in intel.key_dependencies]


def test_well_structured_api_mapper_finds_route(tmp_path):
    root = materialize("well_structured", tmp_path)
    files = [str(p.relative_to(root)).replace("\\", "/") for p in FilesystemScanner(root).scan_code_files()]
    amap = APIMapper(root).build_map(files)
    assert len(amap.routes) >= 1
    # The mapper is a regex-based, per-line extractor: it does not merge an
    # APIRouter(prefix=...) with the route decorator's own path, so the
    # captured path is "/{item_id}", not the fully-resolved "/items/{item_id}".
    assert any("/{item_id}" in r.path for r in amap.routes)


# ── poorly_named: regression guard for the analyzer.py:35 false positive ───


def test_poorly_named_no_false_positive_framework(tmp_path):
    root = materialize("poorly_named", tmp_path)

    profile = ProjectTypeDetector().detect(root)
    assert profile.project_type is not ProjectType.PYTHON_FASTAPI
    assert profile.project_type is ProjectType.PYTHON_GENERIC

    analyzer = CodebaseAnalyzer(root)
    files = [str(p.relative_to(root)).replace("\\", "/") for p in FilesystemScanner(root).scan_code_files()]
    analyzer.classify_architecture_layers(files)
    assert "fastapi" not in analyzer.detected_project_types


# ── legacy_php_style: PHP is now both tagged AND structurally parsed ──────
# (previously a total blind spot — baseline §6.2 — fixed by adding a
# dedicated PHP pattern set to RepositorySnapshotParser._parse_regex).


def test_legacy_php_style_language_tagged_and_parsed(tmp_path):
    root = materialize("legacy_php_style", tmp_path)
    svc = RepositoryCognitionService(root)
    snapshot = svc.index(force=True)

    php_files = [f for f in snapshot.files if f.path.endswith(".php")]
    assert len(php_files) == 3, "all 3 PHP files should be discovered"
    assert all(f.language == RepositoryLanguage.PHP for f in php_files), (
        "PHP must be tagged as its own language, not silently folded into "
        "js_generic or unknown (the pre-consolidation extension table gap)"
    )
    # index.php declares no function/class itself (just require_once calls),
    # but includes/auth.php and includes/db.php each declare one function —
    # both must now be extracted instead of silently yielding zero symbols.
    total_php_symbols = sum(len(f.symbols) for f in php_files)
    assert total_php_symbols > 0, "PHP files must no longer be a total blind spot"


# ── notebook_ml: .ipynb is now discovered and structurally parsed ─────────
# (previously invisible — baseline §6.3 — fixed by adding .ipynb to
# EXTENSION_LANGUAGE_MAP and RepositorySnapshotParser._extract_notebook_source).


def test_notebook_ml_now_discovered_and_parsed(tmp_path):
    root = materialize("notebook_ml", tmp_path)
    discovered = FilesystemScanner(root).scan_code_files()
    discovered_names = {p.name for p in discovered}
    assert "helpers.py" in discovered_names
    assert "train.ipynb" in discovered_names
    assert "explore.ipynb" in discovered_names

    svc = RepositoryCognitionService(root)
    snapshot = svc.index(force=True)
    notebook_files = [f for f in snapshot.files if f.path.endswith(".ipynb")]
    assert len(notebook_files) == 2
    assert all(f.language == RepositoryLanguage.PYTHON for f in notebook_files)
    # The fixture's notebook has two executed code cells (pandas import/read,
    # and a train() function) — both should be extracted as symbols/edges.
    assert any(len(f.symbols) > 0 for f in notebook_files)


# ── ml_pipeline: the oversized vendored file is now opaque, not dominant ───
# (previously the sharpest baseline finding, §6.4 — fixed by the
# MAX_STRUCTURAL_PARSE_BYTES guard in indexer.py/incremental_indexer.py/
# graph_patcher.py).


def test_ml_pipeline_vendored_file_is_opaque_not_dominant(tmp_path):
    root = materialize("ml_pipeline", tmp_path)
    svc = RepositoryCognitionService(root)
    snapshot = svc.index(force=True)

    vendored = next(
        f for f in snapshot.files if f.path.replace("\\", "/").endswith("big_generated.py")
    )
    real_files = [f for f in snapshot.files if f is not vendored]
    real_symbol_count = sum(len(f.symbols) for f in real_files)

    assert vendored.metadata.get("opaque") is True
    assert len(vendored.symbols) == 0, "oversized vendored file must not be structurally parsed"
    assert real_symbol_count > 0, "the real, small files must still be parsed normally"


# ── monorepo: defense-in-depth is a real strength — guard against regressing it ─


def test_monorepo_content_footprint_rescues_nested_manifest(tmp_path):
    root = materialize("monorepo", tmp_path)
    tech = TechnologyDetector(root).detect()
    assert tech.is_monorepo is True
    # Root-only detection cannot see the nested backend-api FastAPI usage.
    assert tech.framework != "FastAPI"

    svc = RepositoryCognitionService(root)
    snapshot = svc.index(force=True)
    frameworks = snapshot.summary["architecture"]["frameworks_detected"]
    assert "FastAPI" in frameworks, (
        "content-based framework-footprint detection must still recover "
        "the nested package's framework even when the root manifest can't see it"
    )


# ── dynamic_imports: layer bucketing works, AND the import graph now sees ──
# the dynamic loader→plugin relationship (previously a total blind spot —
# baseline §6.7 — fixed by RepositorySnapshotParser._extract_dynamic_imports
# + RepositoryGrapher._add_dynamic_prefix_edges).


def test_dynamic_imports_layer_bucketing_and_resolved_edges(tmp_path):
    root = materialize("dynamic_imports", tmp_path)
    svc = RepositoryCognitionService(root)
    snapshot = svc.index(force=True)

    layers = snapshot.summary["architecture"]["layer_membership"]
    plugin_files = [p for p in layers.get("plugins", [])]
    assert any("plugin_a" in p for p in plugin_files)
    assert any("plugin_b" in p for p in plugin_files)

    # importlib.import_module(f"plugins.{name}") has a literal prefix
    # ("plugins") the grapher can expand to every real file under it — a
    # confidence-scored imports_dynamic edge, not a fabricated certain one.
    loader_edges = [e for e in snapshot.edges if "loader.py" in e.source]
    dynamic_targets = {e.target for e in loader_edges if e.edge_type == "imports_dynamic"}
    assert any("plugin_a" in t for t in dynamic_targets)
    assert any("plugin_b" in t for t in dynamic_targets)
    assert all(e.weight < 1.0 for e in loader_edges if e.edge_type == "imports_dynamic")

    # app.js's require(modPath) has no literal information at all — correctly
    # produces no edge (a wrong guess is worse than an honest absence), but
    # the file's dynamic-loading behavior is still recorded as a symbol.
    app_js = next(f for f in snapshot.files if f.path.endswith("app.js"))
    assert any(
        s.metadata.get("dynamic") and s.name == "<dynamic import>" for s in app_js.symbols
    )


# ── mixed_lang_polyglot: still-open gap — tech_stack.language is a single ──
# scalar, so the Rust half of the repo is invisible to it.


def test_mixed_lang_polyglot_single_language_collapse(tmp_path):
    root = materialize("mixed_lang_polyglot", tmp_path)
    tech = TechnologyDetector(root).detect()
    assert tech.language == "Python"
    # Characterizes the still-open gap: a Rust file/Cargo.toml at root
    # produces no trace in tech_stack once Python has already claimed
    # `language` — see todo: make tech_stack fields multi-valued.
    assert "rust" not in tech.to_dict().values()


def test_mixed_lang_polyglot_architecture_detector_handles_missing_pattern(tmp_path):
    root = materialize("mixed_lang_polyglot", tmp_path)
    svc = RepositoryCognitionService(root)
    snapshot = svc.index(force=True)
    # Must degrade to "Unknown" rather than crash or mislabel.
    assert snapshot.summary["architecture"]["arch_report"]["pattern"] == "Unknown"
