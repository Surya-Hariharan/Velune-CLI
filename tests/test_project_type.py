"""Tests for the project type detection system."""

import json
import pytest
from pathlib import Path

from velune.repository.project_type import (
    ProjectType,
    ProjectTypeDetector,
    PROJECT_SYSTEM_PROMPTS,
)


@pytest.fixture
def workspace(tmp_path):
    return tmp_path


def test_detects_fastapi(workspace):
    (workspace / "requirements.txt").write_text("fastapi\nuvicorn\npydantic")
    (workspace / "main.py").write_text("")
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.PYTHON_FASTAPI
    assert "fastapi" in profile.detected_frameworks
    assert profile.primary_language == "python"


def test_detects_nextjs(workspace):
    pkg = {"dependencies": {"next": "14.0.0", "react": "18.0.0"}}
    (workspace / "package.json").write_text(json.dumps(pkg))
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.NODE_NEXTJS
    assert "nextjs" in profile.detected_frameworks


def test_detects_react(workspace):
    pkg = {"dependencies": {"react": "18.0.0", "react-dom": "18.0.0"}}
    (workspace / "package.json").write_text(json.dumps(pkg))
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.NODE_REACT
    assert "react" in profile.detected_frameworks


def test_detects_rust(workspace):
    (workspace / "Cargo.toml").write_text("[package]\nname = 'myapp'\n")
    (workspace / "src").mkdir()
    (workspace / "src" / "main.rs").write_text("fn main() {}")
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.RUST
    assert profile.primary_language == "rust"
    assert "src/main.rs" in profile.entry_points


def test_detects_go(workspace):
    (workspace / "go.mod").write_text("module myapp\ngo 1.21\n")
    (workspace / "main.go").write_text("package main\nfunc main() {}")
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.GO
    assert profile.primary_language == "go"
    assert "main.go" in profile.entry_points


def test_detects_django(workspace):
    (workspace / "requirements.txt").write_text("django\ndjango-rest-framework")
    (workspace / "manage.py").write_text("")
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.PYTHON_DJANGO
    assert "django" in profile.detected_frameworks
    assert "manage.py" in profile.entry_points


def test_detects_flask(workspace):
    (workspace / "requirements.txt").write_text("flask\nflask-sqlalchemy")
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.PYTHON_FLASK
    assert "flask" in profile.detected_frameworks


def test_detects_python_cli(workspace):
    (workspace / "requirements.txt").write_text("typer\nrich")
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.PYTHON_CLI


def test_detects_flutter(workspace):
    (workspace / "pubspec.yaml").write_text("name: myapp\n")
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.FLUTTER
    assert "flutter" in profile.detected_frameworks


def test_detects_dotnet(workspace):
    (workspace / "MyApp.csproj").write_text("<Project/>")
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.DOTNET


def test_detects_java_spring(workspace):
    (workspace / "pom.xml").write_text(
        "<project><dependencies><dependency>"
        "<groupId>org.springframework</groupId>"
        "</dependency></dependencies></project>"
    )
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.JAVA_SPRING
    assert "spring" in profile.detected_frameworks


def test_unknown_project(workspace):
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.UNKNOWN


def test_entry_points_found(workspace):
    (workspace / "requirements.txt").write_text("fastapi")
    (workspace / "main.py").write_text("")
    profile = ProjectTypeDetector().detect(workspace)
    assert "main.py" in profile.entry_points


def test_test_dirs_found(workspace):
    (workspace / "tests").mkdir()
    (workspace / "requirements.txt").write_text("")
    profile = ProjectTypeDetector().detect(workspace)
    assert "tests" in profile.test_directories


def test_config_files_detected(workspace):
    (workspace / "pyproject.toml").write_text("")
    (workspace / "Dockerfile").write_text("")
    profile = ProjectTypeDetector().detect(workspace)
    assert "pyproject.toml" in profile.config_files
    assert "Dockerfile" in profile.config_files


def test_context_hints_python(workspace):
    (workspace / "requirements.txt").write_text("fastapi")
    profile = ProjectTypeDetector().detect(workspace)
    assert len(profile.context_hints) > 0
    assert any("pathlib" in h for h in profile.context_hints)


def test_context_hints_go(workspace):
    (workspace / "go.mod").write_text("module x\ngo 1.21\n")
    profile = ProjectTypeDetector().detect(workspace)
    assert any("error" in h.lower() for h in profile.context_hints)


def test_system_prompt_fastapi(workspace):
    (workspace / "requirements.txt").write_text("fastapi")
    profile = ProjectTypeDetector().detect(workspace)
    assert "FastAPI" in profile.system_prompt_addon
    assert len(profile.system_prompt_addon) > 0


def test_system_prompt_missing_for_unknown(workspace):
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.system_prompt_addon == ""


def test_display_name_set(workspace):
    (workspace / "Cargo.toml").write_text("[package]\n")
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.display_name == "Rust"


def test_react_with_typescript(workspace):
    pkg = {"dependencies": {"react": "18.0.0"}, "devDependencies": {"typescript": "5.0.0"}}
    (workspace / "package.json").write_text(json.dumps(pkg))
    profile = ProjectTypeDetector().detect(workspace)
    assert profile.project_type == ProjectType.NODE_REACT
    assert "typescript" in profile.detected_frameworks


def test_all_project_types_have_display_name():
    from velune.repository.project_type import _DISPLAY_NAMES
    for pt in ProjectType:
        assert pt in _DISPLAY_NAMES, f"Missing display name for {pt}"


def test_all_project_types_have_language():
    from velune.repository.project_type import _LANGUAGE_MAP
    for pt in ProjectType:
        assert pt in _LANGUAGE_MAP, f"Missing language for {pt}"
