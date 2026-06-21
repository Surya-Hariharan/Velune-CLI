"""Tests for ConfigIntelligenceExtractor — config file parsing and context rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from velune.repository.config_intelligence import ConfigIntelligence, ConfigIntelligenceExtractor


class TestConfigIntelligenceDataclass:
    def test_is_empty_when_no_commands(self):
        intel = ConfigIntelligence(project_name="myapp", version="1.0")
        assert intel.is_empty()

    def test_not_empty_when_test_cmd(self):
        intel = ConfigIntelligence(test_cmd="pytest")
        assert not intel.is_empty()

    def test_not_empty_when_deps(self):
        intel = ConfigIntelligence(key_dependencies=["fastapi"])
        assert not intel.is_empty()


class TestParsePyprojectToml:
    def _extractor(self):
        return ConfigIntelligenceExtractor()

    def test_extracts_name_and_version(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nversion = "0.1.0"\n'
        )
        e = self._extractor()
        intel = e._parse_pyproject_toml(tmp_path / "pyproject.toml")
        assert intel.project_name == "myapp"
        assert intel.version == "0.1.0"

    def test_detects_pytest(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\n[tool.pytest.ini_options]\naddopts = "-x -q"\n'
        )
        e = self._extractor()
        intel = e._parse_pyproject_toml(tmp_path / "pyproject.toml")
        assert intel.test_cmd is not None
        assert "pytest" in intel.test_cmd
        assert "-x" in intel.test_cmd

    def test_detects_ruff_lint(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n[tool.ruff]\n')
        e = self._extractor()
        intel = e._parse_pyproject_toml(tmp_path / "pyproject.toml")
        assert intel.lint_cmd == "ruff check ."

    def test_extracts_dependencies(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["fastapi>=0.100", "sqlalchemy", "pydantic"]\n'
        )
        e = self._extractor()
        intel = e._parse_pyproject_toml(tmp_path / "pyproject.toml")
        assert "fastapi" in intel.key_dependencies
        assert "sqlalchemy" in intel.key_dependencies

    def test_dep_count_capped(self, tmp_path):
        deps = [f'"pkg{i}"' for i in range(25)]
        (tmp_path / "pyproject.toml").write_text(
            f'[project]\nname = "x"\ndependencies = [{", ".join(deps)}]\n'
        )
        e = self._extractor()
        intel = e._parse_pyproject_toml(tmp_path / "pyproject.toml")
        assert len(intel.key_dependencies) <= ConfigIntelligenceExtractor.MAX_DEPS

    def test_detects_fastapi_dev_cmd(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["fastapi", "uvicorn"]\n'
        )
        e = self._extractor()
        intel = e._parse_pyproject_toml(tmp_path / "pyproject.toml")
        assert intel.dev_cmd is not None
        assert "uvicorn" in intel.dev_cmd


class TestParsePackageJson:
    def _extractor(self):
        return ConfigIntelligenceExtractor()

    def _write(self, tmp_path, data):
        (tmp_path / "package.json").write_text(json.dumps(data))

    def test_extracts_name_version(self, tmp_path):
        self._write(tmp_path, {"name": "my-app", "version": "2.0.0", "scripts": {}})
        e = self._extractor()
        intel = e._parse_package_json(tmp_path / "package.json")
        assert intel.project_name == "my-app"
        assert intel.version == "2.0.0"

    def test_extracts_test_build_lint(self, tmp_path):
        self._write(
            tmp_path,
            {
                "name": "x",
                "scripts": {
                    "test": "jest",
                    "build": "tsc",
                    "lint": "eslint .",
                    "dev": "next dev",
                },
            },
        )
        e = self._extractor()
        intel = e._parse_package_json(tmp_path / "package.json")
        assert intel.test_cmd == "jest"
        assert intel.build_cmd == "tsc"
        assert intel.lint_cmd == "eslint ."
        assert intel.dev_cmd == "next dev"

    def test_extracts_dependencies(self, tmp_path):
        self._write(
            tmp_path,
            {
                "name": "x",
                "dependencies": {"react": "^18", "next": "^14"},
                "devDependencies": {"typescript": "^5"},
            },
        )
        e = self._extractor()
        intel = e._parse_package_json(tmp_path / "package.json")
        assert "react" in intel.key_dependencies
        assert "next" in intel.key_dependencies
        assert "typescript" in intel.key_dependencies

    def test_scripts_capped(self, tmp_path):
        scripts = {f"script{i}": f"cmd{i}" for i in range(20)}
        self._write(tmp_path, {"name": "x", "scripts": scripts})
        e = self._extractor()
        intel = e._parse_package_json(tmp_path / "package.json")
        assert len(intel.available_scripts) <= ConfigIntelligenceExtractor.MAX_SCRIPTS


class TestParseCargoToml:
    def _extractor(self):
        return ConfigIntelligenceExtractor()

    def test_cargo_default_commands(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "mycrate"\nversion = "0.2.0"\nedition = "2021"\n'
        )
        e = self._extractor()
        intel = e._parse_cargo_toml(tmp_path / "Cargo.toml")
        assert intel.test_cmd == "cargo test"
        assert intel.build_cmd == "cargo build --release"
        assert intel.lint_cmd == "cargo clippy"

    def test_extracts_deps(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text(
            '[package]\nname = "x"\n[dependencies]\nserde = "1"\ntokio = "1"\n'
        )
        e = self._extractor()
        intel = e._parse_cargo_toml(tmp_path / "Cargo.toml")
        assert "serde" in intel.key_dependencies
        assert "tokio" in intel.key_dependencies


class TestParseGoMod:
    def _extractor(self):
        return ConfigIntelligenceExtractor()

    def test_extracts_module_and_go_version(self, tmp_path):
        (tmp_path / "go.mod").write_text(
            "module github.com/example/myservice\n\ngo 1.21\n"
        )
        e = self._extractor()
        intel = e._parse_go_mod(tmp_path / "go.mod")
        assert intel.project_name == "myservice"
        assert intel.version == "1.21"

    def test_default_go_commands(self, tmp_path):
        (tmp_path / "go.mod").write_text("module example.com/app\n\ngo 1.22\n")
        e = self._extractor()
        intel = e._parse_go_mod(tmp_path / "go.mod")
        assert intel.test_cmd == "go test ./..."
        assert intel.build_cmd == "go build ./..."

    def test_extracts_require_deps(self, tmp_path):
        (tmp_path / "go.mod").write_text(
            "module example.com/app\ngo 1.21\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n\tgorm.io/gorm v1.25.1\n)\n"
        )
        e = self._extractor()
        intel = e._parse_go_mod(tmp_path / "go.mod")
        assert "gin" in intel.key_dependencies
        assert "gorm" in intel.key_dependencies


class TestExtractPriorityAndFallback:
    def test_priority_pyproject_over_package(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "py-project"\n[tool.pytest]\n')
        (tmp_path / "package.json").write_text('{"name": "js-project", "scripts": {"test": "jest"}}')
        e = ConfigIntelligenceExtractor()
        intel = e.extract(tmp_path, ["pyproject.toml", "package.json"])
        assert intel.config_source == "pyproject.toml"

    def test_falls_back_when_primary_empty(self, tmp_path):
        # pyproject.toml with no useful data → should try package.json
        (tmp_path / "pyproject.toml").write_text("[build-system]\n")
        (tmp_path / "package.json").write_text(
            '{"name": "js-app", "scripts": {"test": "jest", "build": "vite build"}}'
        )
        e = ConfigIntelligenceExtractor()
        intel = e.extract(tmp_path, ["pyproject.toml", "package.json"])
        assert intel.test_cmd == "jest"

    def test_returns_empty_when_no_config_files(self, tmp_path):
        e = ConfigIntelligenceExtractor()
        intel = e.extract(tmp_path, [])
        assert intel.is_empty()

    def test_handles_malformed_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{not valid json!!!}")
        e = ConfigIntelligenceExtractor()
        intel = e.extract(tmp_path, ["package.json"])
        assert intel.is_empty()

    def test_handles_missing_file(self, tmp_path):
        e = ConfigIntelligenceExtractor()
        intel = e.extract(tmp_path, ["package.json"])  # file doesn't exist
        assert intel.is_empty()


class TestRenderContextBlock:
    def test_empty_intel_returns_empty_string(self):
        e = ConfigIntelligenceExtractor()
        assert e.render_context_block(ConfigIntelligence()) == ""

    def test_contains_test_cmd(self):
        e = ConfigIntelligenceExtractor()
        intel = ConfigIntelligence(test_cmd="pytest -x", config_source="pyproject.toml")
        block = e.render_context_block(intel)
        assert "pytest -x" in block

    def test_contains_project_name(self):
        e = ConfigIntelligenceExtractor()
        intel = ConfigIntelligence(
            project_name="my-project", version="1.0", test_cmd="pytest", config_source="pyproject.toml"
        )
        block = e.render_context_block(intel)
        assert "my-project" in block

    def test_contains_dependencies(self):
        e = ConfigIntelligenceExtractor()
        intel = ConfigIntelligence(key_dependencies=["fastapi", "sqlalchemy"], test_cmd="pytest")
        block = e.render_context_block(intel)
        assert "fastapi" in block
        assert "sqlalchemy" in block

    def test_contains_scripts(self):
        e = ConfigIntelligenceExtractor()
        intel = ConfigIntelligence(
            available_scripts={"deploy": "fly deploy"}, build_cmd="npm run build"
        )
        block = e.render_context_block(intel)
        assert "fly deploy" in block
