"""Filesystem-level Ollama model store — reads Ollama's own manifest format.

Ollama lays models out on disk as an OCI-style registry::

    <root>/
      manifests/<host>/<namespace>/<model>/<tag>   # one JSON manifest per tag
      blobs/sha256-<digest>                          # content-addressed layers

A model's human-readable identity is the manifest *path*, not the blob
filenames (which are opaque sha256 digests). This module reconstructs the exact
names ``ollama list`` shows — e.g. ``qwen2.5-coder:7b``, ``deepseek-coder:6.7b``
— by walking ``manifests/`` and reading the referenced config blob for the
parameter size, quantization, and family. It never guesses a name from a blob
hash, and it works fully offline (no daemon required), which is what makes
custom / external-drive model stores discoverable.

This is deliberately a *read-only* view: the running Ollama daemon remains the
authority for inference. See :mod:`velune.providers.discovery.ollama` for how
this store is layered behind the HTTP API.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("velune.providers.ollama_store")

_DEFAULT_REGISTRY = "registry.ollama.ai"
_DEFAULT_NAMESPACE = "library"

# Ollama's OCI media types we care about.
_MODEL_LAYER = "application/vnd.ollama.image.model"
_PARAMS_LAYER = "application/vnd.ollama.image.params"

_PARAM_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[bB]\b")


@dataclass(slots=True)
class OllamaStoredModel:
    """One model tag discovered on disk via its manifest."""

    name: str  # e.g. "qwen2.5-coder:7b" — exactly as `ollama list` shows it
    root: Path  # the store root this was found under
    size_bytes: int = 0
    parameter_label: str | None = None  # e.g. "7B"
    parameter_count_b: float | None = None
    quantization: str | None = None  # e.g. "Q4_0"
    family: str | None = None  # e.g. "llama"
    context_length: int | None = None  # only when available offline; else None
    metadata: dict = field(default_factory=dict)


class OllamaModelStore:
    """Read-only view over one Ollama model storage root."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).expanduser()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def is_valid_root(path: Path | str) -> bool:
        """True if *path* looks like an Ollama model store.

        Requires both ``manifests/`` and ``blobs/`` subdirectories — the two
        invariants of Ollama's on-disk layout. This rejects an arbitrary folder
        the user might pick by mistake.
        """
        try:
            root = Path(path).expanduser()
            return (root / "manifests").is_dir() and (root / "blobs").is_dir()
        except Exception:
            return False

    def exists(self) -> bool:
        """True if the root is currently reachable (drive mounted, dir present)."""
        try:
            return self.root.exists()
        except OSError:
            # On Windows a disconnected mapped/removable drive raises rather than
            # returning False — treat that as "not reachable".
            return False

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def list_models(self) -> list[OllamaStoredModel]:
        """Return every model tag found under this root (best-effort).

        Individual unreadable/corrupt manifests are skipped with a debug log
        rather than failing the whole scan.
        """
        manifests_dir = self.root / "manifests"
        if not manifests_dir.is_dir():
            return []

        models: list[OllamaStoredModel] = []
        for manifest_path in self._iter_manifest_files(manifests_dir):
            try:
                name = self._reconstruct_name(manifest_path.relative_to(manifests_dir))
                if name is None:
                    continue
                model = self._parse_manifest(manifest_path, name)
                if model is not None:
                    models.append(model)
            except Exception as exc:  # one bad manifest must not abort the scan
                logger.debug("Skipping unreadable manifest %s: %s", manifest_path, exc)
        models.sort(key=lambda m: m.name)
        return models

    @staticmethod
    def _iter_manifest_files(manifests_dir: Path):
        """Yield manifest files (leaf tag files) under ``manifests/``."""
        for path in manifests_dir.rglob("*"):
            # A manifest is a file; directories are the host/namespace/model tree.
            if path.is_file():
                yield path

    @staticmethod
    def _reconstruct_name(rel: Path) -> str | None:
        """Rebuild ``[host/][namespace/]model:tag`` from a manifest's rel path.

        ``rel`` is relative to ``manifests/`` and looks like
        ``<host>/<seg...>/<tag>``. The default ``registry.ollama.ai/library``
        prefix is stripped to match ``ollama list`` output; other registries and
        namespaces are preserved (e.g. ``hf.co/user/model:tag``).
        """
        parts = rel.parts
        if len(parts) < 3:
            # Need at least host / model / tag.
            return None
        host = parts[0]
        tag = parts[-1]
        ns_model = list(parts[1:-1])
        if not ns_model:
            return None

        if host == _DEFAULT_REGISTRY and ns_model[0] == _DEFAULT_NAMESPACE:
            model = "/".join(ns_model[1:])
        elif host == _DEFAULT_REGISTRY:
            model = "/".join(ns_model)
        else:
            model = "/".join([host, *ns_model])

        if not model:
            return None
        return f"{model}:{tag}"

    def _blob_path(self, digest: str) -> Path:
        """Map an OCI ``sha256:<hex>`` digest to its on-disk blob path."""
        normalized = digest.replace(":", "-", 1)
        return self.root / "blobs" / normalized

    def _parse_manifest(self, manifest_path: Path, name: str) -> OllamaStoredModel | None:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        layers = data.get("layers", []) or []

        size_bytes = 0
        params_digest: str | None = None
        for layer in layers:
            mt = layer.get("mediaType", "")
            if mt == _MODEL_LAYER:
                size_bytes = int(layer.get("size", 0) or 0)
            elif mt == _PARAMS_LAYER:
                params_digest = layer.get("digest")

        model = OllamaStoredModel(name=name, root=self.root, size_bytes=size_bytes)

        # Config blob carries parameter size, quantization, and family — all
        # available offline. Missing/unreadable config is non-fatal.
        config_digest = (data.get("config") or {}).get("digest")
        if config_digest:
            self._apply_config(model, config_digest)

        # A params layer, when present, carries num_ctx and similar overrides.
        if params_digest:
            self._apply_params(model, params_digest)

        return model

    def _apply_config(self, model: OllamaStoredModel, config_digest: str) -> None:
        blob = self._blob_path(config_digest)
        if not blob.is_file():
            return
        try:
            cfg = json.loads(blob.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.debug("Unreadable config blob %s: %s", blob, exc)
            return
        model.parameter_label = cfg.get("model_type") or model.parameter_label
        model.quantization = cfg.get("file_type") or model.quantization
        model.family = cfg.get("model_family") or model.family
        if model.parameter_label:
            m = _PARAM_NUM_RE.search(str(model.parameter_label))
            if m:
                model.parameter_count_b = float(m.group(1))
        model.metadata["config"] = {
            "model_format": cfg.get("model_format"),
            "model_families": cfg.get("model_families"),
        }

    def _apply_params(self, model: OllamaStoredModel, params_digest: str) -> None:
        blob = self._blob_path(params_digest)
        if not blob.is_file():
            return
        try:
            params = json.loads(blob.read_text(encoding="utf-8"))
        except Exception:
            return
        # Ollama stores num_ctx as either an int or a string here.
        num_ctx = params.get("num_ctx")
        if num_ctx is not None:
            try:
                model.context_length = int(num_ctx)
            except (TypeError, ValueError):
                pass
