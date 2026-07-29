"""Docker/K8s/CI/Terraform files must actually reach the 'infra' layer bucket.

CodebaseAnalyzer._GENERIC_LAYERS declares an "infra" bucket matching
Dockerfile/*.tf//.github/workflows/, but RepositoryCognitionService only ever
fed it paths from scan_code_files() (CODE_EXTENSIONS-filtered) — YAML/HCL
files and extension-less Dockerfiles never reached the classifier at all.
RepositoryCognitionService._paths_for_layer_classification widens the input
for that one call without touching the code-only grapher/API-map paths.
"""

from __future__ import annotations

from pathlib import Path

from velune.repository.cognition import RepositoryCognitionService


def test_infra_files_reach_the_infra_layer(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def main():\n    pass\n", encoding="utf-8")

    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")

    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "ci.yml").write_text("name: CI\non: [push]\n", encoding="utf-8")

    terraform = tmp_path / "terraform"
    terraform.mkdir()
    (terraform / "main.tf").write_text('resource "null_resource" "x" {}\n', encoding="utf-8")

    service = RepositoryCognitionService(tmp_path)
    code_paths = ["app.py"]

    widened = service._paths_for_layer_classification(code_paths)

    assert "app.py" in widened
    assert "Dockerfile" in widened
    assert ".github/workflows/ci.yml" in widened
    assert "terraform/main.tf" in widened

    layers = service.analyzer.classify_architecture_layers(widened)

    assert set(layers["infra"]) == {"Dockerfile", ".github/workflows/ci.yml", "terraform/main.tf"}


def test_paths_for_layer_classification_degrades_to_code_paths_on_scan_failure(
    tmp_path: Path, monkeypatch
) -> None:
    """A broken scan must not crash indexing — it should just skip the widening."""
    service = RepositoryCognitionService(tmp_path)

    def _boom(self, extensions=None):
        raise OSError("simulated scan failure")

    monkeypatch.setattr(
        "velune.repository.scanner.FilesystemScanner.scan", _boom, raising=True
    )

    result = service._paths_for_layer_classification(["app.py"])

    assert result == ["app.py"]
