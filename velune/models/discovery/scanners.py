"""Local and remote model scanners."""

from __future__ import annotations

import asyncio
import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable, Optional

import httpx
from platformdirs import user_cache_dir

from velune.core.types import ModelDescriptor
from velune.models.discovery.classifier import ModelClassifier
from velune.models.discovery.schemas import DiscoverySource, ModelRecord


class ModelScanner(ABC):
    """Scanner contract used by the discovery service."""

    provider_id: str

    @abstractmethod
    async def discover(self) -> list[ModelRecord]:
        """Discover available models."""


class OllamaScanner(ModelScanner):
    provider_id = "ollama"

    def __init__(self, base_url: str = "http://localhost:11434") -> None:
        self.base_url = base_url
        self.classifier = ModelClassifier()

    async def discover(self) -> list[ModelRecord]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
        except Exception:
            return []

        models = []
        for item in response.json().get("models", []):
            model_id = item.get("name")
            details = item.get("details", {})
            if not model_id:
                continue

            metadata = {
                "raw": item,
                "quantization": _extract_quantization(model_id),
                "context_length": details.get("num_ctx"),
                "parameter_count": details.get("parameter_size"),
                "embedding": any(marker in model_id.lower() for marker in ("embed", "embedding")),
            }
            classification = self.classifier.classify(model_id, self.provider_id, metadata)
            descriptor = ModelDescriptor(
                model_id=model_id,
                provider_id=self.provider_id,
                display_name=model_id,
                context_length=classification.context_length,
                capabilities=classification.capabilities,
                quantization=metadata["quantization"],
                parameter_count_b=_parameter_count_to_billion(details.get("parameter_size")),
                speed_tier=classification.speed_tier,  # type: ignore[arg-type]
                cost_per_1k_tokens=None,
                tags=["local", "ollama"],
                metadata=metadata,
                is_local=True,
            )
            models.append(ModelRecord(descriptor=descriptor, source=DiscoverySource.OLLAMA, classification=classification, location=self.base_url, metadata=metadata))
        return models


class LMStudioScanner(ModelScanner):
    provider_id = "lm_studio"

    def __init__(self, base_url: str = "http://localhost:1234") -> None:
        self.base_url = base_url
        self.classifier = ModelClassifier()

    async def discover(self) -> list[ModelRecord]:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/v1/models")
                response.raise_for_status()
        except Exception:
            return []

        models = []
        for item in response.json().get("data", []):
            model_id = item.get("id")
            if not model_id:
                continue

            metadata = {"raw": item, "embedding": any(marker in model_id.lower() for marker in ("embed", "embedding"))}
            classification = self.classifier.classify(model_id, self.provider_id, metadata)
            descriptor = ModelDescriptor(
                model_id=model_id,
                provider_id=self.provider_id,
                display_name=model_id,
                context_length=classification.context_length,
                capabilities=classification.capabilities,
                quantization=item.get("quantization"),
                speed_tier=classification.speed_tier,  # type: ignore[arg-type]
                cost_per_1k_tokens=None,
                tags=["local", "lm-studio"],
                metadata=metadata,
                is_local=True,
            )
            models.append(ModelRecord(descriptor=descriptor, source=DiscoverySource.LM_STUDIO, classification=classification, location=self.base_url, metadata=metadata))
        return models


class GGUFScanner(ModelScanner):
    provider_id = "gguf"

    def __init__(self, search_roots: Optional[Iterable[Path]] = None) -> None:
        self.search_roots = list(search_roots or self._default_roots())
        self.classifier = ModelClassifier()

    async def discover(self) -> list[ModelRecord]:
        return await asyncio.to_thread(self._discover_sync)

    def _discover_sync(self) -> list[ModelRecord]:
        records: list[ModelRecord] = []
        for root in self.search_roots:
            if not root.exists():
                continue
            for file_path in root.rglob("*.gguf"):
                metadata = self._metadata_from_file(file_path)
                model_id = metadata["model_id"]
                classification = self.classifier.classify(model_id, self.provider_id, metadata)
                descriptor = ModelDescriptor(
                    model_id=model_id,
                    provider_id=self.provider_id,
                    display_name=model_id,
                    context_length=classification.context_length,
                    capabilities=classification.capabilities,
                    quantization=metadata.get("quantization"),
                    vram_required_gb=metadata.get("vram_required_gb"),
                    parameter_count_b=metadata.get("parameter_count_b"),
                    speed_tier=classification.speed_tier,  # type: ignore[arg-type]
                    cost_per_1k_tokens=None,
                    tags=["local", "gguf"],
                    metadata=metadata,
                    is_local=True,
                )
                records.append(ModelRecord(descriptor=descriptor, source=DiscoverySource.GGUF, classification=classification, location=str(file_path), metadata=metadata))
        return records

    def _metadata_from_file(self, file_path: Path) -> dict[str, Any]:
        quantization = _extract_quantization(file_path.name)
        return {
            "model_id": file_path.stem,
            "path": str(file_path),
            "quantization": quantization,
            "context_length": _context_length_hint(file_path.name),
            "parameter_count_b": _parameter_count_hint(file_path.name),
            "vram_required_gb": _vram_hint(file_path.name, quantization),
        }

    def _default_roots(self) -> list[Path]:
        roots = [Path.cwd(), Path.home(), Path.home() / "Downloads"]
        model_dir = os.getenv("MODEL_DIR")
        if model_dir:
            roots.append(Path(model_dir))
        hf_cache = Path(user_cache_dir("huggingface", "huggingface"))
        roots.append(hf_cache)
        return [root for root in roots if str(root)]


class HuggingFaceScanner(ModelScanner):
    provider_id = "huggingface"

    def __init__(self, search_roots: Optional[Iterable[Path]] = None) -> None:
        self.search_roots = list(search_roots or self._default_roots())
        self.classifier = ModelClassifier()

    async def discover(self) -> list[ModelRecord]:
        return await asyncio.to_thread(self._discover_sync)

    def _discover_sync(self) -> list[ModelRecord]:
        records: list[ModelRecord] = []
        for root in self.search_roots:
            if not root.exists():
                continue
            for config_file in root.rglob("config.json"):
                if not self._looks_like_local_model(config_file.parent):
                    continue
                metadata = self._metadata_from_dir(config_file.parent, config_file)
                model_id = metadata["model_id"]
                classification = self.classifier.classify(model_id, self.provider_id, metadata)
                descriptor = ModelDescriptor(
                    model_id=model_id,
                    provider_id=self.provider_id,
                    display_name=model_id,
                    context_length=classification.context_length,
                    capabilities=classification.capabilities,
                    quantization=metadata.get("quantization"),
                    vram_required_gb=metadata.get("vram_required_gb"),
                    parameter_count_b=metadata.get("parameter_count_b"),
                    speed_tier=classification.speed_tier,  # type: ignore[arg-type]
                    cost_per_1k_tokens=None,
                    tags=["local", "huggingface"],
                    metadata=metadata,
                    is_local=True,
                )
                records.append(ModelRecord(descriptor=descriptor, source=DiscoverySource.HUGGINGFACE, classification=classification, location=str(config_file.parent), metadata=metadata))
        return records

    def _metadata_from_dir(self, directory: Path, config_file: Path) -> dict[str, Any]:
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except Exception:
            config = {}

        model_id = config.get("model_type") or directory.name
        return {
            "model_id": model_id,
            "path": str(directory),
            "config": config,
            "context_length": config.get("max_position_embeddings"),
            "quantization": config.get("quantization_config", {}).get("quant_method") if isinstance(config.get("quantization_config"), dict) else None,
            "embedding": any(marker in model_id.lower() for marker in ("embed", "embedding", "bge", "e5", "gte")),
        }

    def _looks_like_local_model(self, directory: Path) -> bool:
        return any((directory / candidate).exists() for candidate in ("model.safetensors", "pytorch_model.bin", "model.gguf", "tokenizer.json"))

    def _default_roots(self) -> list[Path]:
        return [Path.home() / ".cache" / "huggingface" / "hub", Path.home() / ".cache" / "huggingface" / "transformers"]


def _extract_quantization(name: str) -> Optional[str]:
    quant_map = {
        "q4_k_m": "Q4_K_M",
        "q4_0": "Q4_0",
        "q5_k_m": "Q5_K_M",
        "q5_0": "Q5_0",
        "q8_0": "Q8_0",
        "fp16": "FP16",
        "f16": "F16",
    }
    lowered = name.lower()
    for marker, quantization in quant_map.items():
        if marker in lowered:
            return quantization
    return None


def _parameter_count_to_billion(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value) / 1_000_000_000
    return None


def _context_length_hint(name: str) -> int:
    lowered = name.lower()
    if "128k" in lowered:
        return 128000
    if "32k" in lowered:
        return 32000
    if "16k" in lowered:
        return 16000
    if "8k" in lowered:
        return 8192
    return 4096


def _parameter_count_hint(name: str) -> Optional[float]:
    lowered = name.lower()
    if "70b" in lowered:
        return 70.0
    if "34b" in lowered:
        return 34.0
    if "13b" in lowered:
        return 13.0
    if "7b" in lowered:
        return 7.0
    if "3b" in lowered:
        return 3.0
    return None


def _vram_hint(name: str, quantization: Optional[str]) -> Optional[float]:
    parameter_count = _parameter_count_hint(name)
    if parameter_count is None:
        return None
    factor_map = {None: 2.0, "FP16": 2.0, "F16": 2.0, "Q8_0": 1.0, "Q5_K_M": 0.7, "Q5_0": 0.7, "Q4_K_M": 0.5, "Q4_0": 0.5}
    factor = factor_map.get(quantization, 0.5)
    return (parameter_count * factor)