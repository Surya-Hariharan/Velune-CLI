"""Tests for filesystem-based Ollama discovery: manifest name reconstruction,
metadata extraction, the location registry, and layered (API + disk) discovery.

A synthetic ``manifests/`` + ``blobs/`` tree stands in for a real Ollama store,
so these run with no daemon and no network.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from velune.providers.ollama_locations import OllamaLocationRegistry
from velune.providers.ollama_store import OllamaModelStore


def _write_blob(root: Path, payload: dict) -> str:
    """Write *payload* as a blob and return its ``sha256:<hex>`` digest."""
    raw = json.dumps(payload).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    blob = root / "blobs" / f"sha256-{digest}"
    blob.parent.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(raw)
    return f"sha256:{digest}"


def _make_model(
    root: Path,
    *,
    host: str,
    namespace: str,
    model: str,
    tag: str,
    model_type: str = "7B",
    file_type: str = "Q4_0",
    family: str = "llama",
    size: int = 4_000_000_000,
    num_ctx: int | None = None,
) -> None:
    """Create a manifest + config (+ optional params) blob for one model tag."""
    config_digest = _write_blob(
        root,
        {
            "model_format": "gguf",
            "model_family": family,
            "model_families": [family],
            "model_type": model_type,
            "file_type": file_type,
        },
    )
    layers = [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:deadbeef", "size": size}]
    if num_ctx is not None:
        params_digest = _write_blob(root, {"num_ctx": num_ctx})
        layers.append(
            {"mediaType": "application/vnd.ollama.image.params", "digest": params_digest, "size": 32}
        )
    manifest = {
        "schemaVersion": 2,
        "config": {"mediaType": "application/vnd.docker.container.image.v1+json", "digest": config_digest, "size": 100},
        "layers": layers,
    }
    mpath = root / "manifests" / host / namespace / model / tag
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    root = tmp_path / "ollama" / "models"
    (root / "blobs").mkdir(parents=True)
    _make_model(
        root,
        host="registry.ollama.ai",
        namespace="library",
        model="qwen2.5-coder",
        tag="7b",
        model_type="7.6B",
        file_type="Q4_K_M",
        family="qwen2",
        num_ctx=32768,
    )
    _make_model(
        root,
        host="registry.ollama.ai",
        namespace="library",
        model="deepseek-coder",
        tag="6.7b",
    )
    # Custom namespace + custom registry to exercise name reconstruction.
    _make_model(root, host="registry.ollama.ai", namespace="myuser", model="custom", tag="latest")
    _make_model(root, host="hf.co", namespace="someone", model="cool-model", tag="q8")
    return root


class TestManifestStore:
    def test_is_valid_root(self, store_root: Path, tmp_path: Path) -> None:
        assert OllamaModelStore.is_valid_root(store_root) is True
        assert OllamaModelStore.is_valid_root(tmp_path) is False

    def test_reconstructs_real_names(self, store_root: Path) -> None:
        names = {m.name for m in OllamaModelStore(store_root).list_models()}
        assert "qwen2.5-coder:7b" in names  # library/ stripped
        assert "deepseek-coder:6.7b" in names
        assert "myuser/custom:latest" in names  # non-default namespace kept
        assert "hf.co/someone/cool-model:q8" in names  # non-default registry kept

    def test_never_uses_blob_hashes(self, store_root: Path) -> None:
        for m in OllamaModelStore(store_root).list_models():
            assert "sha256" not in m.name

    def test_extracts_metadata_from_config(self, store_root: Path) -> None:
        by_name = {m.name: m for m in OllamaModelStore(store_root).list_models()}
        qwen = by_name["qwen2.5-coder:7b"]
        assert qwen.quantization == "Q4_K_M"
        assert qwen.parameter_label == "7.6B"
        assert qwen.parameter_count_b == pytest.approx(7.6)
        assert qwen.family == "qwen2"
        assert qwen.context_length == 32768  # from the params layer
        assert qwen.size_bytes > 0

    def test_missing_context_is_none(self, store_root: Path) -> None:
        by_name = {m.name: m for m in OllamaModelStore(store_root).list_models()}
        assert by_name["deepseek-coder:6.7b"].context_length is None

    def test_disconnected_root(self) -> None:
        store = OllamaModelStore("Z:/definitely/not/here")
        assert store.exists() is False
        assert store.list_models() == []


class TestLocationRegistry:
    def test_add_validate_dedup_remove(self, store_root: Path, tmp_path: Path) -> None:
        reg = OllamaLocationRegistry(path=tmp_path / "locations.json")
        # reject non-store dir
        assert reg.add(tmp_path).ok is False
        # accept valid store
        assert reg.add(store_root).ok is True
        # idempotent
        before = len(reg.load())
        assert reg.add(store_root).ok is True
        assert len(reg.load()) == before
        # remove
        assert reg.remove(store_root) is True
        assert reg.load() == []

    def test_reject_missing_path(self, tmp_path: Path) -> None:
        reg = OllamaLocationRegistry(path=tmp_path / "locations.json")
        assert reg.add(tmp_path / "nope").ok is False

    def test_resolution_order_and_env(self, store_root: Path, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv("OLLAMA_MODELS", str(store_root))
        reg = OllamaLocationRegistry(path=tmp_path / "locations.json")
        sources = [rr.source for rr in reg.resolve_roots()]
        # registered first (none here), then env, then defaults
        assert "env:OLLAMA_MODELS" in sources
        # the env root is reachable + valid → an active store
        active = [str(s.root) for s in reg.active_stores()]
        assert str(store_root) in active

    def test_disconnected_detection(self, tmp_path: Path) -> None:
        reg = OllamaLocationRegistry(path=tmp_path / "locations.json")
        # Register a real store, then make it vanish (simulating an unplugged drive).
        root = tmp_path / "ext" / "models"
        (root / "manifests").mkdir(parents=True)
        (root / "blobs").mkdir(parents=True)
        assert reg.add(root).ok is True
        import shutil

        shutil.rmtree(tmp_path / "ext")
        disconnected = reg.disconnected()
        assert len(disconnected) == 1
        assert disconnected[0].disconnected is True


class TestLayeredDiscovery:
    @pytest.mark.asyncio
    async def test_filesystem_fallback_when_daemon_down(
        self, store_root: Path, tmp_path: Path, monkeypatch
    ) -> None:
        # Point discovery at our synthetic store and force "daemon down".
        monkeypatch.setenv("OLLAMA_MODELS", str(store_root))
        monkeypatch.setattr(
            OllamaLocationRegistry, "__init__", lambda self, path=None: setattr(self, "_path", tmp_path / "loc.json")
        )

        from velune.providers.discovery import ollama as ollama_mod

        async def _not_running(cls, base_url=None):  # noqa: ANN001
            return False

        monkeypatch.setattr(ollama_mod.OllamaDiscovery, "is_running", classmethod(_not_running))

        models = await ollama_mod.OllamaDiscovery().discover()
        names = {m.model_id for m in models}
        assert "qwen2.5-coder:7b" in names
        # daemon down → everything flagged not servable, with a reason
        for m in models:
            assert m.metadata.get("servable") is False
            assert m.metadata.get("servable_reason")
