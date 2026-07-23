"""Tests for the read-only Ollama on-disk manifest store."""

from __future__ import annotations

import json

from velune.providers.ollama_store import OllamaModelStore


def _write_blob(root, digest: str, payload: dict) -> None:
    blob_dir = root / "blobs"
    blob_dir.mkdir(parents=True, exist_ok=True)
    normalized = digest.replace(":", "-", 1)
    (blob_dir / normalized).write_text(json.dumps(payload), encoding="utf-8")


def _write_manifest(
    root, rel_parts: tuple[str, ...], config_digest: str, params_digest: str | None, size: int
) -> None:
    manifest_dir = root / "manifests" / "/".join(rel_parts[:-1])
    manifest_dir.mkdir(parents=True, exist_ok=True)
    layers = [{"mediaType": "application/vnd.ollama.image.model", "size": size}]
    if params_digest:
        layers.append({"mediaType": "application/vnd.ollama.image.params", "digest": params_digest})
    manifest = {"config": {"digest": config_digest}, "layers": layers}
    (manifest_dir / rel_parts[-1]).write_text(json.dumps(manifest), encoding="utf-8")


def test_default_library_namespace_is_stripped(tmp_path):
    _write_blob(
        tmp_path,
        "sha256:cfg1",
        {"model_type": "8B", "file_type": "Q4_K_M", "model_family": "llama"},
    )
    _write_manifest(
        tmp_path,
        ("registry.ollama.ai", "library", "llama3", "8b"),
        "sha256:cfg1",
        None,
        size=4_000_000_000,
    )

    models = OllamaModelStore(tmp_path).list_models()
    assert len(models) == 1
    assert models[0].name == "llama3:8b"
    assert models[0].parameter_count_b == 8.0
    assert models[0].quantization == "Q4_K_M"
    assert models[0].family == "llama"
    assert models[0].size_bytes == 4_000_000_000


def test_non_default_registry_and_namespace_preserved(tmp_path):
    _write_blob(tmp_path, "sha256:cfg2", {"model_type": "7B"})
    _write_manifest(
        tmp_path,
        ("hf.co", "someuser", "some-model", "latest"),
        "sha256:cfg2",
        None,
        size=1,
    )

    models = OllamaModelStore(tmp_path).list_models()
    assert models[0].name == "hf.co/someuser/some-model:latest"


def test_params_layer_provides_context_length(tmp_path):
    _write_blob(tmp_path, "sha256:cfg3", {"model_type": "7B"})
    _write_blob(tmp_path, "sha256:params1", {"num_ctx": 32768})
    _write_manifest(
        tmp_path,
        ("registry.ollama.ai", "library", "qwen2.5", "7b"),
        "sha256:cfg3",
        "sha256:params1",
        size=1,
    )

    models = OllamaModelStore(tmp_path).list_models()
    assert models[0].context_length == 32768


def test_corrupt_manifest_is_skipped_not_fatal(tmp_path):
    good_dir = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "good"
    good_dir.mkdir(parents=True)
    _write_blob(tmp_path, "sha256:cfggood", {"model_type": "3B"})
    (good_dir / "latest").write_text(
        json.dumps({"config": {"digest": "sha256:cfggood"}, "layers": []}), encoding="utf-8"
    )

    bad_dir = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "latest").write_text("{not valid json", encoding="utf-8")

    models = OllamaModelStore(tmp_path).list_models()
    names = [m.name for m in models]
    assert "good:latest" in names
    assert "bad:latest" not in names


def test_exists_false_for_missing_root(tmp_path):
    store = OllamaModelStore(tmp_path / "nope")
    assert store.exists() is False
    assert store.list_models() == []
